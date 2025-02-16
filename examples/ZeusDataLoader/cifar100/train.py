# Copyright (C) 2023 Jae-Won Chung <jwnchung@umich.edu>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Example script for running Zeus on a CIFAR100 job."""

import random
import argparse

import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

# ZEUS
from zeus.run import ZeusDataLoader
from zeus.monitor import ZeusMonitor
from zeus.optimizer.power_limit import MaxSlowdownConstraint
from zeus.optimizer import GlobalPowerLimitOptimizer
from zeus.util.env import get_env

from models import all_models, get_model


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--arch",
        metavar="ARCH",
        default="shufflenetv2",
        choices=all_models,
        help="Model architecture: " + " | ".join(all_models),
    )
    parser.add_argument(
        "--epochs", type=int, default=100, help="Maximum number of epochs to train."
    )
    parser.add_argument(
        "--batch_size", type=int, default=128, help="Batch size."
    )
    parser.add_argument(
        "--num_workers", type=int, default=4, help="Number of workers in dataloader."
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Random seed to use for training."
    )
    parser.add_argument(
        "--profile", type=bool, default=None, help="Whether or not to run profiling"
    )
    parser.add_argument(
        "--profile_path", type=str, default=None, help="Path for profiling"
    )
    parser.add_argument(
        "--power_limits", type=int, nargs="+", help="Power limit for GPU", required=True
    )
    parser.add_argument(
        "--gpu_index", type=int, default=None, help="Which GPU is being run (0 is stronger, 1 is weaker)"
    )
    parser.add_argument(
        "--gpu_split", type=int, default=100, help="Smaller percentage to be trained (0 to 100)"
    )
    parser.add_argument(
        "--warmup_steps", type=int, default=5, help="Warm up steps for profiling"
    )
    parser.add_argument(
        "--profile_steps", type=int, default=10, help="Profile steps"
    )

    # ZEUS
    runtime_mode = parser.add_mutually_exclusive_group()
    runtime_mode.add_argument(
        "--zeus", action="store_true", help="Whether to run Zeus."
    )

    return parser.parse_args()


def main(args: argparse.Namespace) -> None:
    """Run the main training routine."""
    # Set random seed.
    if args.seed is not None:
        set_seed(args.seed)

    # Prepare model.
    # NOTE: Using torchvision.models would be also straightforward. For example:
    #       model = vars(torchvision.models)[args.arch](num_classes=100)
    model = get_model(args.arch)

    train_dataset = datasets.CIFAR100(
        root="data",
        train=True,
        download=True,
        transform=transforms.Compose(
            [
                transforms.RandomCrop(32, padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.RandomRotation(15),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=(0.5070751592371323, 0.48654887331495095, 0.4409178433670343),
                    std=(0.2673342858792401, 0.2564384629170883, 0.27615047132568404),
                ),
            ]
        ),
    )

    # If training first GPU
    if args.gpu_index != None and args.gpu_index == 0:
        limit = int(len(train_dataset) * (args.gpu_split / 100))
        train_dataset = torch.utils.data.Subset(train_dataset, range(0, limit))

    # If training second GPU
    elif args.gpu_index != None and args.gpu_index == 1:
        limit = int(len(train_dataset) * (args.gpu_split / 100))
        train_dataset = torch.utils.data.Subset(train_dataset, range(limit, len(train_dataset)))
    

    val_dataset = datasets.CIFAR100(
        root="data",
        train=False,
        download=True,
        transform=transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=(0.5070751592371323, 0.48654887331495095, 0.4409178433670343),
                    std=(0.2673342858792401, 0.2564384629170883, 0.27615047132568404),
                ),
            ]
        ),
    )

    # ZEUS
    # Prepare dataloaders.
    if args.zeus:
        # Zeus
        train_loader = ZeusDataLoader(
            train_dataset,
            max_epochs=args.epochs,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=args.num_workers,
        )
        val_loader = ZeusDataLoader(
            val_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=args.num_workers,
        )
    else:
        train_loader = DataLoader(
            train_dataset,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=args.num_workers,
        )
        val_loader = DataLoader(
            val_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=args.num_workers,
        )

    # Send model to CUDA.
    model = model.cuda()

    # Prepare loss function and optimizer.
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adadelta(model.parameters())

    monitor = ZeusMonitor(gpu_indices=[0])

    plo = GlobalPowerLimitOptimizer(
        monitor=monitor,
        optimum_selector=MaxSlowdownConstraint(
            factor=get_env("ZEUS_MAX_SLOWDOWN", float, 1.1),
        ),
        warmup_steps=args.warmup_steps,
        profile_steps=args.profile_steps,
        pl_step=50,
        profile_path=args.profile_path,
        power_limits=args.power_limits,
    )

    # ZEUS
    # ZeusDataLoader may early stop training when the cost is expected
    # to exceed the cost upper limit or the target metric was reached.
    if args.zeus:
        assert isinstance(train_loader, ZeusDataLoader)
        epoch_iter = train_loader.epochs()
    else:
        epoch_iter = range(args.epochs)

    monitor.begin_window("cifar_training")

    # Main training loop.
    for epoch in epoch_iter:
        train(train_loader, model, criterion, optimizer, epoch, args, plo)
        if args.profile:
            plo.on_epoch_end()

        acc = validate(val_loader, model, criterion, epoch, args)

        # ZEUS
        if args.zeus:
            assert isinstance(train_loader, ZeusDataLoader)
            train_loader.report_metric(acc, higher_is_better=True)

    measurement = monitor.end_window("cifar_training")
    print(f"Time (s): {measurement.time}")
    print(f"Energy (J): {measurement.total_energy}")


def train(train_loader, model, criterion, optimizer, epoch, args, power_limit_optimizer):
    """Train the model for one epoch."""
    model.train()
    length = len(train_loader)
    num_samples = length * args.batch_size

    for batch_index, (images, labels) in enumerate(train_loader):
        if args.profile:
            power_limit_optimizer.on_step_begin()
        labels = labels.cuda()
        images = images.cuda()

        optimizer.zero_grad()
        outputs = model(images)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()

        print(
            f"Training Epoch: {epoch} [{(batch_index + 1) * args.batch_size}/{num_samples}]"
            f"\tLoss: {loss.item():0.4f}"
        )

@torch.no_grad()
def validate(val_loader, model, criterion, epoch, args):
    """Evaluate the model on the validation set."""
    model.eval()

    test_loss = 0.0
    correct = 0
    num_samples = len(val_loader) * args.batch_size

    for images, labels in val_loader:
        images = images.cuda()
        labels = labels.cuda()

        outputs = model(images)
        loss = criterion(outputs, labels)

        test_loss += loss.item()
        _, preds = outputs.max(1)
        correct += preds.eq(labels).sum().item()

    print(
        f"Validation Epoch: {epoch}, Average loss: {test_loss / num_samples:.4f}"
        f", Accuracy: {correct / num_samples:.4f}"
    )

    return correct / num_samples


def set_seed(seed: int) -> None:
    """Set random seed for reproducible results."""
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


if __name__ == "__main__":
    main(parse_args())
