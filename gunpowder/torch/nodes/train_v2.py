import logging
import os.path
import shutil

import numpy as np

from gunpowder.array import ArrayKey, Array
from gunpowder.array_spec import ArraySpec
from gunpowder.ext import torch, wandb, tensorboardX, NoSuchModule
from gunpowder.nodes.generic_train import GenericTrain
from datetime import datetime

from typing import Dict, Union, Optional
# experimental to wrap ddp
from accelerate import Accelerator


logger = logging.getLogger(__name__)


class Train(GenericTrain):
    """Torch implementation of :class:`gunpowder.nodes.GenericTrain`.

    Args:

        model (subclass of ``torch.nn.Module``):

            The model to train.

        loss:

            The torch loss to use.

        optimizer:

            The torch optimizer to use.

        inputs (``dict``, ``string`` -> :class:`ArrayKey`):

            Dictionary from the names of input tensors (argument names of the
            ``forward`` method) in the model to array keys.

        loss_inputs (``dict``, ``string`` or ``int`` -> :class:`ArrayKey`):

            Dictionary with the names of input variables to the loss function as
            keys, and ArrayKeys containing the desired data as values. Keys can
            be either strings or integers. If the key is an integer, it will
            be treated as a positional argument to the loss function, a
            string will be used as a named argument

        outputs (``dict``, ``string`` or ``int`` -> :class:`ArrayKey`):

            Dictionary from the names of tensors in the network to array
            keys. If the key is a string, the tensor will be retrieved
            by checking the model for an attribute with they key as its name.
            If the key is an integer, it is interpreted as a tuple index of
            the outputs of the network.
            New arrays will be generated by this node for each entry (if
            requested downstream).

        array_specs (``dict``, :class:`ArrayKey` -> :class:`ArraySpec`, optional):

            Used to set the specs of generated arrays (at the moment only
            ``output``). This is useful to set the ``voxel_size``, for example,
            if they differ from the voxel size of the input arrays. Only fields
            that are not ``None`` in the given :class:`ArraySpec` will be used.

        checkpoint_basename (``string``, optional):

            The basename used for checkpoint files. Defaults to ``model``.

        save_every (``int``, optional):

            After how many iterations to create a checkpoint to store the
            learnt weights.

        log_dir (``string``, optional):

            Directory for saving tensorboard summaries.

        log_every (``int``, optional):

            After how many iterations to write out tensorboard summaries.

        spawn_subprocess (``bool``, optional):

            Whether to run the ``train_step`` in a separate process. Default is false.

        device (``str``, optional):

            Accepts a cuda gpu specifically to train on, helps in multi-card systems.
            defaults to ``cuda``

        checkpoint_folder(``str``, optional):

            Path to checkpoint folder when training multiple models simultaneously.
            defaults to ``current folder``.

        delete_checkpoints(``bool``, optional):

            Deletes all previous checkpoints for fresh training
            defaults to ``False``.

        use_wandb (``bool``, optional):

            Whether to use Weights and Biases `WandB` for logging training loss. Default is False.

        wandb_project_name (``str``, optional):
            Specify a  wandb project name when using `WandB`. Default is `dummy_project`

        use_ddp (``bool``, optional):
            Train on multiple gpus. Experimental feature implemented via accelerator library from hugging face.
            Default is `False`.

    """

    def __init__(
            self,
            model,
            loss,
            optimizer,
            inputs: Dict[str, ArrayKey],
            outputs: Dict[Union[int, str], ArrayKey],
            loss_inputs: Dict[Union[int, str], ArrayKey],
            gradients: Dict[Union[int, str], ArrayKey] = {},
            array_specs: Optional[Dict[ArrayKey, ArraySpec]] = None,
            checkpoint_basename: str = "model",
            save_every: int = 2000,
            log_dir: str = None,
            log_every: int = 1,
            spawn_subprocess: bool = False,
            device: str = "cuda",
            checkpoint_folder: str = "./",
            delete_checkpoints: bool = False,
            use_wandb=False,
            project_name='dummy_project',
            use_ddp=False

    ):

        if not model.training:
            logger.warning(
                "Model is in evaluation mode during training. "
                "Consider using model.train()"
            )

        # not yet implemented
        gradients = gradients
        inputs.update(
            {k: v for k, v in loss_inputs.items() if v not in outputs.values()}
        )

        super(Train, self).__init__(
            inputs, outputs, gradients, array_specs, spawn_subprocess=spawn_subprocess
        )

        self.model = model
        self.loss = loss
        self.optimizer = optimizer
        self.loss_inputs = loss_inputs
        self.checkpoint_basename = checkpoint_basename
        self.save_every = save_every
        self.dev = device
        self.checkpoint_folder = checkpoint_folder
        self.use_wandb = use_wandb
        self.iteration = 0
        self.use_ddp = use_ddp

        # Initialize Accelerator for distributed training
        if self.use_ddp:
            accelerator = Accelerator()

        # defaults to wandb logging in offline mode
        # TODO: use tensorboard via Wandb??
        if self.use_wandb:
            if not isinstance(wandb, NoSuchModule) and log_dir is not None:
                # wandb does not create auto directory. If dir does not exist, it will log in `/temp or /tmp` folder
                if not os.path.exists(log_dir):
                    os.makedirs(log_dir, exist_ok=True)
                self.wandb_config = {}
                # create a dummy name with timestamp
                trainer_name = f"wandb_offline_{datetime.now():%Y-%m-%d_%H-%M-%S}"
                # initialize wandb run and synced to `http://localhost:8080/{<mohinta2892>` with a dummy project name
                # mode = online, but logs flow to local instance
                # if mode= offline, logs will be saved on disk and will need to sync to wandb.ai or local instance by
                # running `wandb sync --project_name <project_name> -p <path/to/logs>
                self.wandb_logger = wandb.init(
                    project=project_name, name=trainer_name, dir=log_dir,  # mode="offline",
                    config=self.wandb_config, resume="allow"
                )
                self.log_every = log_every

                # if wandb is being used, upload the model params to wandb too
                wandb.watch(self.model, log="parameters", log_freq=self.save_every, log_graph=True)

            else:
                self.wandb_logger = None
                if log_dir is not None:
                    logger.warning("log_dir given, but wandb is not installed")
        else:
            if not isinstance(tensorboardX, NoSuchModule) and log_dir is not None:
                self.summary_writer = tensorboardX.SummaryWriter(log_dir)
                self.log_every = log_every
            else:
                self.summary_writer = None
                if log_dir is not None:
                    logger.warning("log_dir given, but tensorboardX is not installed")

        self.intermediate_layers = {}
        self.register_hooks()

        if not os.path.exists(self.checkpoint_folder):
            print(f"Making checkpoint folder at: {self.checkpoint_folder}")
            os.makedirs(self.checkpoint_folder)
        elif delete_checkpoints:
            print(f"Re-making checkpoint folder at: {self.checkpoint_folder}")
            shutil.rmtree(self.checkpoint_folder)
            os.makedirs(self.checkpoint_folder)

    def register_hooks(self):
        for key in self.outputs:
            if isinstance(key, str):
                layer = getattr(self.model, key)
                layer.register_forward_hook(self.create_hook(key))

    def create_hook(self, key):
        def save_layer(module, input, output):
            self.intermediate_layers[key] = output

        return save_layer

    def retain_gradients(self, request, outputs):
        for array_name, array_key in self.gradients.items():
            if array_key not in request:
                continue
            if isinstance(array_name, int):
                tensor = outputs[array_name]
            elif isinstance(array_name, str):
                tensor = getattr(self.model, array_name)
            else:
                raise RuntimeError(
                    "only ints and strings are supported as gradients keys"
                )
            tensor.retain_grad()

    def start(self):

        self.use_cuda = torch.cuda.is_available()
        self.device = torch.device(self.dev if self.use_cuda else "cpu")

        try:
            self.model = self.model.to(self.device)
        except RuntimeError as e:
            raise RuntimeError(
                "Failed to move model to device. If you are using a child process "
                "to run your model, maybe you already initialized CUDA by sending "
                "your model to device in the main process."
            ) from e
        if isinstance(self.loss, torch.nn.Module):
            self.loss = self.loss.to(self.device)

        checkpoint, self.iteration = self._get_latest_checkpoint(
            os.path.join(self.checkpoint_folder, self.checkpoint_basename)
        )

        if checkpoint is not None:

            logger.info("Resuming training from iteration %d", self.iteration)
            logger.info("Loading %s", checkpoint)

            checkpoint = torch.load(checkpoint, map_location=self.device)
            self.model.load_state_dict(checkpoint["model_state_dict"])
            self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])

        else:

            logger.info("Starting training from scratch")

        logger.info("Using device %s", self.device)

    def train_step(self, batch, request):

        inputs = self.__collect_provided_inputs(batch)
        requested_outputs = self.__collect_requested_outputs(request)

        if self.ddp:
            # Prepare the model and optimizer
            self.model, self.optimizer = accelerator.prepare(self.model, self.optimizer)

        if self.ddp :
            # keys are argument names of model forward pass
            # set to accelerator device
            device_inputs = {
                k: torch.as_tensor(v, device=accelerator.device) for k, v in inputs.items()
            }
        else:
            # keys are argument names of model forward pass
            device_inputs = {
                k: torch.as_tensor(v, device=self.device) for k, v in inputs.items()
            }

        # get outputs. Keys are tuple indices or model attr names as in self.outputs
        self.optimizer.zero_grad()
        if self.ddp:
            with accelerator.autocast():  # Mixed precision training if enabled
                model_outputs = self.model(**device_inputs)
                
                if isinstance(model_outputs, tuple):
                    outputs = {i: model_outputs[i] for i in range(len(model_outputs))}
                elif isinstance(model_outputs, torch.Tensor):
                    outputs = {0: model_outputs}
                else:
                    raise RuntimeError(
                        "Torch train node only supports return types of tuple",
                        f"and torch.Tensor from model.forward(). not {type(model_outputs)}",
                    )
        else:
            model_outputs = self.model(**device_inputs)
            if isinstance(model_outputs, tuple):
                outputs = {i: model_outputs[i] for i in range(len(model_outputs))}
            elif isinstance(model_outputs, torch.Tensor):
                outputs = {0: model_outputs}
            else:
                raise RuntimeError(
                    "Torch train node only supports return types of tuple",
                    f"and torch.Tensor from model.forward(). not {type(model_outputs)}",
                )
        outputs.update(self.intermediate_layers)

        # Some inputs to the loss should come from the batch, not the model
        provided_loss_inputs = self.__collect_provided_loss_inputs(batch)

        if self.ddp:
            device_loss_inputs = {
            k: torch.as_tensor(v, device=accelerator.device)
            for k, v in provided_loss_inputs.items()}
        else:
            device_loss_inputs = {
                k: torch.as_tensor(v, device=self.device)
                for k, v in provided_loss_inputs.items()
            }

        # Some inputs to the loss function should come from the outputs of the model
        # Update device loss inputs with tensors from outputs if available
        flipped_outputs = {v: outputs[k] for k, v in self.outputs.items()}
        device_loss_inputs = {
            k: flipped_outputs.get(v, device_loss_inputs.get(k))
            for k, v in self.loss_inputs.items()
        }

        device_loss_args = []
        for i in range(len(device_loss_inputs)):
            if i in device_loss_inputs:
                device_loss_args.append(device_loss_inputs.pop(i))
            else:
                break
        device_loss_kwargs = {}
        for k, v in list(device_loss_inputs.items()):
            if isinstance(k, str):
                device_loss_kwargs[k] = device_loss_inputs.pop(k)
        assert (
                len(device_loss_inputs) == 0
        ), f"Not all loss inputs could be interpreted. Failed keys: {device_loss_inputs.keys()}"

        self.retain_gradients(request, outputs)

        logger.debug(
            "model outputs: %s",
            {k: v.shape for k, v in outputs.items()})
        logger.debug(
            "loss inputs: %s %s",
            [v.shape for v in device_loss_args],
            {k: v.shape for k, v in device_loss_kwargs.items()})
        loss = self.loss(*device_loss_args, **device_loss_kwargs)

        if self.ddp:
            # do accelerator backward for loss backwards
            accelerator.backward(loss)
        else:
            loss.backward()

        # take an optimizer step
        self.optimizer.step()

        # add requested model outputs to batch
        for array_key, array_name in requested_outputs.items():
            spec = self.spec[array_key].copy()
            spec.roi = request[array_key].roi
            batch.arrays[array_key] = Array(
                outputs[array_name].cpu().detach().numpy(), spec
            )

        for array_name, array_key in self.gradients.items():
            if array_key not in request:
                continue
            if isinstance(array_name, int):
                tensor = outputs[array_name]
            elif isinstance(array_name, str):
                tensor = getattr(self.model, array_name)
            else:
                raise RuntimeError(
                    "only ints and strings are supported as gradients keys"
                )
            spec = self.spec[array_key].copy()
            spec.roi = request[array_key].roi
            batch.arrays[array_key] = Array(
                tensor.grad.cpu().detach().numpy(), spec
            )

        for array_key, array_name in requested_outputs.items():
            spec = self.spec[array_key].copy()
            spec.roi = request[array_key].roi
            batch.arrays[array_key] = Array(
                outputs[array_name].cpu().detach().numpy(), spec
            )

        batch.loss = loss.cpu().detach().numpy()
        self.iteration += 1
        batch.iteration = self.iteration

        if batch.iteration % self.save_every == 0:
            # let's keep torch checkpointing for now

            checkpoint_name = self._checkpoint_name(
                self.checkpoint_basename, batch.iteration
            )

            logger.info("Creating checkpoint %s", checkpoint_name)
            torch.save(
                {
                    "model_state_dict": self.model.state_dict(),
                    "optimizer_state_dict": self.optimizer.state_dict(),
                },
                os.path.join(self.checkpoint_folder, checkpoint_name),
            )
            
            # save the latest batch as 'latest' too; overwritten every-time
            checkpoint_name = self._checkpoint_name(
            self.checkpoint_basename, 'latest'
            )

            logger.info("Creating checkpoint %s", checkpoint_name)

            torch.save(
                {
                    "model_state_dict": self.model.state_dict(),
                    "optimizer_state_dict": self.optimizer.state_dict(),
                },
                os.path.join(self.checkpoint_folder, checkpoint_name),
            )

        if self.use_wandb and self.wandb_logger and batch.iteration % self.log_every == 0:
            # TODO: use watch to track gradients as well??
            self.wandb_config.update({"loss": batch.loss, "iteration": batch.iteration})
            self.wandb_logger.log(self.wandb_config)

        elif self.summary_writer and batch.iteration % self.log_every == 0:
            self.summary_writer.add_scalar("loss", batch.loss, batch.iteration)

    def __collect_requested_outputs(self, request):

        array_outputs = {}

        for output_name, array_key in self.outputs.items():
            if array_key in request:
                array_outputs[array_key] = output_name

        return array_outputs

    def __collect_provided_inputs(self, batch):

        return self.__collect_provided_arrays(
            {k: v for k, v in self.inputs.items() if k not in self.loss_inputs}, batch
        )

    def __collect_provided_loss_inputs(self, batch):

        return self.__collect_provided_arrays(
            self.loss_inputs, batch, expect_missing_arrays=True
        )

    def __collect_provided_arrays(self, reference, batch, expect_missing_arrays=False):

        arrays = {}

        for array_name, array_key in reference.items():
            if isinstance(array_key, ArrayKey):
                msg = f"batch does not contain {array_key}, array {array_name} will not be set"
                if array_key in batch.arrays:
                    arrays[array_name] = batch.arrays[array_key].data
                elif not expect_missing_arrays:
                    logger.warn(msg)
                else:
                    logger.debug(msg)
            elif isinstance(array_key, np.ndarray):
                arrays[array_name] = array_key
            elif isinstance(array_key, str):
                arrays[array_name] = getattr(batch, array_key)
            else:
                raise Exception(
                    "Unknown network array key {}, can't be given to "
                    "network".format(array_key)
                )

        return arrays