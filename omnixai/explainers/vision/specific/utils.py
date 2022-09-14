#
# Copyright (c) 2022 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
import numpy as np
from omnixai.utils.misc import is_tf_available, is_torch_available
from omnixai.data.image import Image


class GradMixin:

    @staticmethod
    def _resize(preprocess_function, image):
        """
        Rescales the raw input image to the input size of the model.

        :param preprocess_function: The preprocessing function.
        :param image: The raw input image.
        :return: The resized image.
        """
        assert image.shape[0] == 1, "`image` can contain one instance only."
        if preprocess_function is None:
            return image

        y = image.to_numpy()
        x = preprocess_function(image)
        if not isinstance(x, np.ndarray):
            try:
                x = x.detach().cpu().numpy()
            except:
                x = x.numpy()
        x = x.squeeze()
        if x.shape[0] == 3:
            x = np.transpose(x, (1, 2, 0))

        min_a, max_a = np.min(y), np.max(y)
        min_b, max_b = np.min(x), np.max(x)
        r = (max_a - min_a) / (max_b - min_b + 1e-8)
        return Image(data=(r * x + min_a - r * min_b).astype(int), batched=False, channel_last=True)


def _smooth_grad_torch(
        X,
        y,
        model,
        preprocess_function,
        mode: str,
        num_samples: int,
        sigma: float
):
    import torch

    model.eval()
    device = next(model.parameters()).device
    inputs = preprocess_function(X) if preprocess_function is not None else X.to_numpy()
    inputs = inputs if isinstance(inputs, torch.Tensor) else \
        torch.tensor(inputs, dtype=torch.get_default_dtype())

    outputs = model(inputs.to(device))
    if mode == "classification":
        if y is not None:
            if type(y) == int:
                y = [y for _ in range(len(X))]
            else:
                assert len(X) == len(y), (
                    f"Parameter ``y`` is a {type(y)}, the length of y "
                    f"should be the same as the number of images in X."
                )
        else:
            scores = outputs.detach().cpu().numpy()
            y = np.argmax(scores, axis=1).astype(int)
    else:
        y = None

    gradients = 0
    idx = torch.arange(outputs.shape[0])
    input_images = inputs.detach().cpu().numpy()
    sigma = sigma * (np.max(input_images) - np.min(input_images))
    for i in range(num_samples):
        noise = np.random.randn(*inputs.shape) * sigma
        x = torch.tensor(
            input_images + noise,
            dtype=torch.get_default_dtype(),
            device=device,
            requires_grad=True
        )
        outputs = model(x.to(device))
        if y is not None:
            outputs = outputs[idx, y]
        grad = torch.autograd.grad(torch.unbind(outputs), x)[0]
        gradients += grad.detach().cpu().numpy()

    gradients = gradients / num_samples
    gradients = np.transpose(gradients, (0, 2, 3, 1))
    return gradients, y


def _smooth_grad_tf(
        X,
        y,
        model,
        preprocess_function,
        mode: str,
        num_samples,
        sigma: float
):
    import tensorflow as tf

    inputs = preprocess_function(X) if preprocess_function is not None else X.to_numpy()
    inputs = tf.convert_to_tensor(inputs)
    if mode == "classification":
        if y is not None:
            if type(y) == int:
                y = [y for _ in range(len(X))]
            else:
                assert len(X) == len(y), (
                    f"Parameter ``y`` is a {type(y)}, the length of y "
                    f"should be the same as the number of images in X."
                )
        else:
            predictions = model(inputs)
            y = tf.argmax(predictions, axis=-1).numpy().astype(int)
    else:
        y = None

    gradients = 0
    input_images = inputs.numpy()
    sigma = sigma * (np.max(input_images) - np.min(input_images))
    for i in range(num_samples):
        noise = np.random.randn(*inputs.shape) * sigma
        x = tf.Variable(
            noise + input_images,
            dtype=tf.float32,
            trainable=True
        )
        with tf.GradientTape() as tape:
            tape.watch(x)
            outputs = model(x)
            if y is not None:
                outputs = tf.reshape(tf.gather(outputs, y, axis=1), shape=(-1,))
            grad = tape.gradient(outputs, x)
            gradients += grad.numpy()

    gradients = gradients / num_samples
    return gradients, y


def smooth_grad(
        X,
        y,
        model,
        preprocess_function,
        mode: str,
        num_samples: int,
        sigma: float
):
    if is_torch_available():
        import torch.nn as nn

        if isinstance(model, nn.Module):
            return _smooth_grad_torch(
                X=X,
                y=y,
                model=model,
                preprocess_function=preprocess_function,
                mode=mode,
                num_samples=num_samples,
                sigma=sigma
            )

    if is_tf_available():
        import tensorflow as tf

        if isinstance(model, tf.keras.Model):
            return _smooth_grad_tf(
                X=X,
                y=y,
                model=model,
                preprocess_function=preprocess_function,
                mode=mode,
                num_samples=num_samples,
                sigma=sigma
            )

    raise ValueError(f"`model` should be a tf.keras.Model "
                     f"or a torch.nn.Module instead of {type(model)}")


def _guided_bp_torch(
        X,
        y,
        model,
        preprocess_function,
        mode: str,
        num_samples: int,
        sigma: float
):
    import torch

    model.eval()
    device = next(model.parameters()).device
    inputs = preprocess_function(X) if preprocess_function is not None else X.to_numpy()
    inputs = inputs if isinstance(inputs, torch.Tensor) else \
        torch.tensor(inputs, dtype=torch.get_default_dtype())

    outputs = model(inputs.to(device))
    if mode == "classification":
        if y is not None:
            if type(y) == int:
                y = [y for _ in range(len(X))]
            else:
                assert len(X) == len(y), (
                    f"Parameter ``y`` is a {type(y)}, the length of y "
                    f"should be the same as the number of images in X."
                )
        else:
            scores = outputs.detach().cpu().numpy()
            y = np.argmax(scores, axis=1).astype(int)
    else:
        y = None

    hooks = []
    relu_outputs = []

    def _forward_hook(_module, _inputs, _outputs):
        relu_outputs.append(_outputs)

    def _backward_hook(_module, _inputs, _outputs):
        activations = relu_outputs[-1]
        activations[activations > 0] = 1
        modified_grad_out = activations * torch.clamp(_inputs[0], min=0.0)
        del relu_outputs[-1]
        return (modified_grad_out,)

    try:
        pass
    except Exception as e:
        raise e
    finally:
        for hook in hooks:
            hook.remove()
