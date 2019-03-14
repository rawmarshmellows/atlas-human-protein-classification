from enum import Enum
from collections import defaultdict
from functools import partial

import torch
from torch.utils.data import DataLoader
from torch.optim import Adam
import matplotlib.pyplot as plt

from fastprogress.fastprogress import progress_bar, master_bar

from pytorch_toolbox.training import callbacks
from pytorch_toolbox.training.data import DataBunch
from pytorch_toolbox.training.optimizer import OptimWrapper
from pytorch_toolbox.training.defaults import *
from pytorch_toolbox.utils import listify, to_numpy, if_none, is_listy, range_of, flatten_model, \
    to_detach, requires_grad


class Phase(Enum):
    TRAIN = 1
    VAL = 2
    TEST = 3


def determine_phase(train, last_target, label_key="label"):
    if train:
        return Phase.TRAIN
    else:
        label = last_target.get(label_key)
        if label is not None:
            return Phase.VAL
        else:
            return Phase.TEST


AdamW = partial(Adam, betas=(0.9, 0.99))


class Learner:
    def __init__(self, data: DataBunch, model: nn.Module, loss_func: Callable, opt_func: Callable = AdamW,
                 metrics: Collection[Callable] = None, true_weight_decay: bool = True,
                 batch_norm_weight_decay: bool = True, weight_decay: Floats = 1e-2, train_bn: bool = True,
                 model_dir: str = "model", callback_fns: Collection[Callable] = None,
                 callbacks: Collection[Callable] = [], layer_groups: Collection[nn.Module] = None):
        self.data = data
        self.model = model.to(self.data.device)
        self.loss_func = loss_func
        self.opt_func = opt_func
        self.true_weight_decay = true_weight_decay
        self.batch_norm_weight_decay = batch_norm_weight_decay
        self.weight_decay = weight_decay
        self.train_bn = train_bn
        self.model_dir = model_dir
        self.metrics = listify(metrics)
        self.callbacks = listify(callbacks)
        self.callback_fns = [Recorder] + listify(callback_fns)

        if not layer_groups:
            self.layer_groups = [nn.Sequential(*flatten_model(self.model))]

    def fit(self, epochs: int, lr: Union[Floats, slice] = default_lr,
            wd: Floats = None, callbacks: Collection[callbacks.Callback] = None) -> None:
        "Fit the model on this learner with `lr` learning rate, `wd` weight decay for `epochs` with `callbacks`."
        lr = self.lr_range(lr)
        if wd is None: wd = self.wd
        self.create_opt(lr, wd)
        callbacks = [cb(self) for cb in self.callback_fns] + listify(callbacks)
        fit(epochs, self.model, self.loss_func, opt=self.opt, data=self.data, metrics=self.metrics,
            callbacks=self.callbacks + callbacks)

    def create_opt(self, lr: Floats, wd: Floats = 0.) -> None:
        "Create optimizer with `lr` learning rate and `wd` weight decay."
        self.opt = OptimWrapper.create(self.opt_func, lr, self.layer_groups, wd=wd, true_wd=self.true_wd,
                                       bn_wd=self.bn_wd)

    def model_gradients(self):
        for lg in self.layer_groups:
            for l in lg:
                print(l)
                for p in l.parameters():
                    print(p.shape)
                    print(p.requires_grad)

    def predict_on_dl(self, dl, pbar=None, callbacks=None, callback_fns=None, metrics=None):
        assert dl is not None
        metrics = if_none(metrics, self.metrics)
        callbacks_fns = [cb(self) for cb in if_none(callback_fns, [])]
        cb_handler = callbacks.CallbackHandler(self.callbacks + if_none(callbacks, []) + callbacks_fns, metrics)
        with torch.no_grad():
            self.model.eval()
            for xb, yb in progress_bar(dl, parent=pbar, leave=(pbar is not None)):
                if cb_handler: xb, yb = cb_handler.on_batch_begin(xb, yb, train=False)
                cb_handler = if_none(cb_handler, callbacks.CallbackHandler())
                if not is_listy(xb): xb = [xb]
                out = self.model(*xb)
                _ = cb_handler.on_loss_begin(out)

    def predict_on_test_dl(self, pbar=None, callbacks=None, metrics=None):
        """Test with callbacks"""
        dl = self.data.test_dl
        self.predict_on_dl(dl, pbar, callbacks, metrics)

    def freeze(self) -> None:
        "Freeze up to last layer."
        assert (len(self.layer_groups) > 1)
        self.freeze_to(-1)

    def unfreeze(self):
        "Unfreeze entire model."
        self.freeze_to(0)

    def freeze_layer_groups(self, layer_group_idxs):
        if not is_listy(layer_group_idxs): layer_group_idxs = [layer_group_idxs]
        super().unfreeze()
        for i in layer_group_idxs:
            for l in self.layer_groups[i]:
                if not self.train_bn or not isinstance(l, bn_types):
                    requires_grad(l, False)

    def unfreeze_layer_groups(self, layer_group_idxs):
        if not is_listy(layer_group_idxs): layer_group_idxs = [layer_group_idxs]
        layer_group_idxs_to_freeze = list(set(list(range(len(self.layer_groups)))) - set(layer_group_idxs))
        self.freeze_layer_groups(layer_group_idxs_to_freeze)

    def load_from_path(self, path, device=None):
        if device is None: device = self.data.device
        self.model.load_state_dict(torch.load(path, map_location=device))


def fit(epochs: int, model: nn.Module, loss_func: LossFunction, opt: optim.Optimizer,
        data: DataBunch, callbacks: Optional[callbacks.CallbackList] = None, metrics: OptionalMetrics = None) -> None:
    "Fit the `model` on `data` and learn using `loss` and `opt`."
    cb_handler = callbacks.CallbackHandler(callbacks, metrics)
    pbar = master_bar(range(epochs))
    cb_handler.on_train_begin(epochs, pbar=pbar, metrics=metrics)

    exception = False
    try:
        for epoch in pbar:
            model.train()
            cb_handler.on_epoch_begin()

            for xb, yb in progress_bar(data.train_dl, parent=pbar):
                xb, yb = cb_handler.on_batch_begin(xb, yb)
                loss = loss_batch(model, xb, yb, loss_func, opt, cb_handler)
                if cb_handler.on_batch_end(loss): break

            if hasattr(data, 'valid_dl') and data.valid_dl is not None:
                val_loss = validate(model, data.valid_dl, loss_func=loss_func,
                                    cb_handler=cb_handler, pbar=pbar)
            else:
                val_loss = None
            if cb_handler.on_epoch_end(val_loss): break
    except Exception as e:
        exception = e
        raise e
    finally:
        cb_handler.on_train_end(exception)


def validate(model: nn.Module, dl: DataLoader, loss_func: OptionalLossFunction = None,
             cb_handler: Optional[callbacks.CallbackHandler] = None,
             pbar: Optional[PBar] = None, average=True, n_batch: Optional[int] = None) -> Iterator[
    Tuple[Union[Tensor, int], ...]]:
    "Calculate loss and metrics for the validation set."
    model.eval()
    with torch.no_grad():
        val_losses, nums = [], []
        for xb, yb in progress_bar(dl, parent=pbar, leave=(pbar is not None)):
            if cb_handler: xb, yb = cb_handler.on_batch_begin(xb, yb, train=False)
            val_losses.append(loss_batch(model, xb, yb, loss_func, cb_handler=cb_handler))
            if not is_listy(yb): yb = [yb]
            nums.append(yb[0].shape[0])
            if cb_handler and cb_handler.on_batch_end(val_losses[-1]): break
            if n_batch and (len(nums) >= n_batch): break
        nums = np.array(nums, dtype=np.float32)
        if average:
            return (to_numpy(torch.stack(val_losses)) * nums).sum() / nums.sum()
        else:
            return val_losses


def loss_batch(model: nn.Module, xb: Tensor, yb: Tensor, loss_func: OptionalLossFunction = None,
               opt: OptOptimizer = None,
               cb_handler: Optional[callbacks.CallbackHandler] = None) -> Tuple[Union[Tensor, int, float, str]]:
    "Calculate loss and metrics for a batch, call out to callbacks as necessary."
    cb_handler = if_none(cb_handler, callbacks.CallbackHandler())
    if not is_listy(xb):
        xb = [xb]
    if not is_listy(yb):
        yb = [yb]
    out = model(*xb)
    out = cb_handler.on_loss_begin(out)

    if not loss_func: return to_detach(out), yb[0].detach()
    loss = loss_func(out, *yb)

    if opt is not None:
        loss = cb_handler.on_backward_begin(loss)
        loss.backward()
        cb_handler.on_backward_end()
        opt.step()
        cb_handler.on_step_end()
        opt.zero_grad()

    return loss.detach().cpu()


class BaseRecorder(callbacks.LearnerCallback):
    "A `LearnerCallback` that records epoch, loss, opt and metric data during training."
    _order = -10

    def __init__(self, learn: Learner):
        super().__init__(learn)
        self.opt = self.learn.opt
        self.train_dl = self.learn.data.train_dl

    def on_train_begin(self, pbar: PBar, metrics_names: Collection[str], **kwargs: Any) -> None:
        "Initialize recording status at beginning of training."
        self.pbar = pbar
        self.names = ['epoch', 'train_loss', 'valid_loss'] + metrics_names
        if hasattr(self, '_added_met_names'): self.names += self._added_met_names
        self.pbar.write('  '.join(self.names), table=True)
        self.losses, self.val_losses, self.lrs, self.moms, self.metrics, self.nb_batches = [], [], [], [], [], []

    def on_batch_begin(self, train, **kwargs: Any) -> None:
        "Record learning rate and momentum at beginning of batch."
        if train:
            self.lrs.append(self.opt.lr)
            self.moms.append(self.opt.mom)

    def on_backward_begin(self, smooth_loss: Tensor, **kwargs: Any) -> None:
        "Record the loss before any other callback has a chance to modify it."
        self.losses.append(smooth_loss)
        if self.pbar is not None and hasattr(self.pbar, 'child'):
            self.pbar.child.comment = f'{smooth_loss:.4f}'

    def on_epoch_end(self, epoch: int, num_batch: int, smooth_loss: Tensor,
                     last_metrics=MetricsList, **kwargs: Any) -> bool:
        "Save epoch info: num_batch, smooth_loss, metrics."
        self.nb_batches.append(num_batch)
        if last_metrics is not None:
            self.val_losses.append(last_metrics[0])
            if hasattr(self, '_added_mets'): last_metrics += self._added_mets
            if len(last_metrics) > 1: self.metrics.append(last_metrics[1:])
            self.format_stats([epoch, smooth_loss] + last_metrics)
        else:
            self.format_stats([epoch, smooth_loss])
        return False

    def format_stats(self, stats: TensorOrNumberList) -> None:
        "Format stats before printing."
        str_stats = []
        for name, stat in zip(self.names, stats):
            t = str(stat) if isinstance(stat, int) else f'{stat:.6f}'
            t += ' ' * (len(name) - len(t))
            str_stats.append(t)
        self.pbar.write('  '.join(str_stats), table=True)

    def add_metrics(self, metrics):
        self._added_mets = metrics

    def add_metric_names(self, names):
        self._added_met_names = names

    def plot_lr(self, show_moms=False) -> None:
        "Plot learning rate, `show_moms` to include momentum."
        iterations = range_of(self.lrs)
        if show_moms:
            _, axs = plt.subplots(1, 2, figsize=(12, 4))
            axs[0].plot(iterations, self.lrs)
            axs[1].plot(iterations, self.moms)
        else:
            plt.plot(iterations, self.lrs)

    def plot(self, skip_start: int = 10, skip_end: int = 5) -> None:
        "Plot learning rate and losses, trimmed between `skip_start` and `skip_end`."
        lrs = self.lrs[skip_start:-skip_end] if skip_end > 0 else self.lrs[skip_start:]
        losses = self.losses[skip_start:-skip_end] if skip_end > 0 else self.losses[skip_start:]
        _, ax = plt.subplots(1, 1)
        ax.plot(lrs, losses)
        ax.set_ylabel("Loss")
        ax.set_xlabel("Learning Rate")
        ax.set_xscale('log')
        ax.xaxis.set_major_formatter(plt.FormatStrFormatter('%.0e'))

    def plot_losses(self) -> None:
        "Plot training and validation losses."
        _, ax = plt.subplots(1, 1)
        iterations = range_of(self.losses)
        ax.plot(iterations, self.losses)
        val_iter = self.nb_batches
        val_iter = np.cumsum(val_iter)
        ax.plot(val_iter, self.val_losses)

    def plot_metrics(self) -> None:
        "Plot metrics collected during training."
        assert len(self.metrics) != 0, "There are no metrics to plot."
        _, axes = plt.subplots(len(self.metrics[0]), 1, figsize=(6, 4 * len(self.metrics[0])))
        val_iter = self.nb_batches
        val_iter = np.cumsum(val_iter)
        axes = axes.flatten() if len(self.metrics[0]) != 1 else [axes]
        for i, ax in enumerate(axes):
            values = [met[i] for met in self.metrics]
            ax.plot(val_iter, values)


class Recorder(BaseRecorder):
    """A extended recorder which has the ability to record the the losses and metric per epoch,
    this is so that we can use the average value of the losses to determine whether a model is good,
     or if and when to do early stopping/reduce LR"""
    _order = -10

    def __init__(self, learn: Learner):
        super().__init__(learn)
        self.loss_history = defaultdict(lambda: defaultdict(list))
        self.metric_history = defaultdict(lambda: defaultdict(list))
        self.phase = None

    @property
    def history(self):
        return {**self.loss_history, **self.metric_history}

    def on_batch_begin(self, train, epoch, last_target, **kwargs):
        super().on_batch_begin(train, **kwargs)
        self.phase = determine_phase(train, last_target)
        self.key = (self.phase.name, epoch)

    def _create_loss_values_for_batch_for_every_samples(self):
        per_sample_loss_values_for_current_batch = dict()
        for loss in self.learn.loss_func.losses:
            name = loss.__class__.__name__
            per_sample_loss = loss.per_sample_loss
            per_sample_loss_values_for_current_batch[f"{name}"] = per_sample_loss
        return per_sample_loss_values_for_current_batch

    def _update_loss_history(self, loss_for_current_batch):
        for name, loss_value in loss_for_current_batch.items():
            self.loss_history[self.key][name].extend(to_numpy(loss_value))

    def on_batch_end(self, **kwargs):
        super().on_batch_end(**kwargs)
        average_loss_for_current_batch = self._create_loss_values_for_batch_for_every_samples()
        self._update_loss_history(average_loss_for_current_batch)

    def on_epoch_end(self, epoch, num_batch, smooth_loss, last_metrics, **kwargs):
        super().on_epoch_end(epoch, num_batch, smooth_loss, last_metrics, **kwargs)
        if self.phase == Phase.VAL:
            metric_names = self.names[3:]
            for name, metric in zip(metric_names, self.metrics[0]):
                self.metric_history[self.key][name].append(metric.item())
