import argparse
import glob
import json
import multiprocessing
import os
import random
import re
from importlib import import_module
from pathlib import Path
import wandb
import torchvision
import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.optim.lr_scheduler import StepLR, ReduceLROnPlateau, CosineAnnealingLR
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from dataset import MaskBaseDataset, BaseAugmentation
from loss import create_criterion
from sklearn.model_selection import StratifiedKFold
from dataset import TestDataset

import pandas as pd
from torchvision.transforms import Resize, ToTensor, Normalize

import torch.nn as nn
from model import build_model
from torch.optim import Adam
import wandb


def getDataloader(dataset, train_idx, valid_idx, batch_size, num_workers):
    # ?Έ?λ‘? ? ?¬λ°μ?? dataset?? train_idx? ?΄?Ή?? Subset μΆμΆ
    train_set = torch.utils.data.Subset(dataset,
                                        indices=train_idx)
    # ?Έ?λ‘? ? ?¬λ°μ?? dataset?? valid_idx? ?΄?Ή?? Subset μΆμΆ
    val_set = torch.utils.data.Subset(dataset,
                                      indices=valid_idx)

    # μΆμΆ? Train Subset?Όλ‘? DataLoader ??±
    train_loader = torch.utils.data.DataLoader(
        train_set,
        batch_size=batch_size,
        num_workers=num_workers,
        drop_last=True,
        shuffle=True
    )
    # μΆμΆ? Valid Subset?Όλ‘? DataLoader ??±
    val_loader = torch.utils.data.DataLoader(
        val_set,
        batch_size=batch_size,
        num_workers=num_workers,
        drop_last=True,
        shuffle=False
    )

    # ??±? DataLoader λ°ν
    return train_loader, val_loader


def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def grid_image(np_images, gts, preds, n=16, shuffle=False):
    batch_size = np_images.shape[0]
    assert n <= batch_size

    choices = random.choices(
        range(batch_size), k=n) if shuffle else list(range(n))
    # cautions: hardcoded, ?΄λ―Έμ?? ?¬κΈ°μ ?°?Ό figsize λ₯? μ‘°μ ?΄?Ό ?  ? ??΅??€. T.T
    figure = plt.figure(figsize=(12, 18 + 2))
    # cautions: hardcoded, ?΄λ―Έμ?? ?¬κΈ°μ ?°?Ό top λ₯? μ‘°μ ?΄?Ό ?  ? ??΅??€. T.T
    plt.subplots_adjust(top=0.8)
    n_grid = int(np.ceil(n ** 0.5))
    tasks = ["mask", "gender", "age"]
    for idx, choice in enumerate(choices):
        gt = gts[choice].item()
        pred = preds[choice].item()
        image = np_images[choice]
        gt_decoded_labels = MaskBaseDataset.decode_multi_class(gt)
        pred_decoded_labels = MaskBaseDataset.decode_multi_class(pred)
        title = "\n".join([
            f"{task} - gt: {gt_label}, pred: {pred_label}"
            for gt_label, pred_label, task
            in zip(gt_decoded_labels, pred_decoded_labels, tasks)
        ])

        plt.subplot(n_grid, n_grid, idx + 1, title=title)
        plt.xticks([])
        plt.yticks([])
        plt.grid(False)
        plt.imshow(image, cmap=plt.cm.binary)

    return figure


def increment_path(path, exist_ok=False):
    """ Automatically increment path, i.e. runs/exp --> runs/exp0, runs/exp1 etc.

    Args:
        path (str or pathlib.Path): f"{model_dir}/{args.name}".
        exist_ok (bool): whether increment path (increment if False).
    """
    path = Path(path)
    if (path.exists() and exist_ok) or (not path.exists()):
        return str(path)
    else:
        dirs = glob.glob(f"{path}*")
        matches = [re.search(rf"%s(\d+)" % path.stem, d) for d in dirs]
        i = [int(m.groups()[0]) for m in matches if m]
        n = max(i) + 1 if i else 2
        return f"{path}{n}"


def initialize_weights(model):
    """
    Xavier uniform λΆν¬λ‘? λͺ¨λ  weight λ₯? μ΄κΈ°??©??€.
    ? λ§μ?? weight μ΄κΈ°? λ°©λ²??? ?€? λ¬Έμ?? μ°Έκ³ ?΄μ£ΌμΈ?. https://pytorch.org/docs/stable/nn.init.html
    """
    for m in model.modules():
        if isinstance(m, nn.Conv2d):
            init.xavier_uniform_(m.weight.data)
            if m.bias is not None:
                m.bias.data.zero_()
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
        elif isinstance(m, nn.Linear):
            m.weight.data.normal_(0, 0.01)
            m.bias.data.zero_()


def train(data_dir, model_dir, args):
    seed_everything(args.seed)

    # -- test dataset
    test_img_root = '/opt/ml/input/data/eval/'
    # public, private ??€?Έ??΄ μ‘΄μ¬?? κ°κ°? ?μΈ‘κ²°κ³Όλ?? ????₯?©??€.
    # meta ?°?΄?°??? ?΄λ―Έμ?? κ²½λ‘λ₯? λΆλ¬?΅??€.
    submission = pd.read_csv(os.path.join(test_img_root, 'info.csv'))
    test_image_dir = os.path.join(test_img_root, 'images')
    # Test Dataset ?΄??€ κ°μ²΄λ₯? ??±?κ³? DataLoaderλ₯? λ§λ­??€.
    test_image_paths = [os.path.join(test_image_dir, img_id)
                        for img_id in submission.ImageID]
    test_dataset = TestDataset(test_image_paths, resize=args.resize)
    test_loader = DataLoader(
        test_dataset,
        shuffle=False
    )

    num_workers = multiprocessing.cpu_count() // 2

    train_log_interval = 20  # logging?  iteration? μ£ΌκΈ°

    # -- dataset
    dataset_module = getattr(import_module("dataset"),
                             args.dataset)  # default: MaskBaseDataset
    dataset = dataset_module(
        data_dir=data_dir,
    )
    num_classes = dataset.num_classes  # 18

    # -- augmentation
    transform_module = getattr(import_module(
        "dataset"), args.augmentation)  # default: BaseAugmentation
    transform = transform_module(
        resize=args.resize,
        mean=dataset.mean,
        std=dataset.std,
    )
    dataset.set_transform(transform)

    save_dir = increment_path(os.path.join(model_dir, args.name))
    os.makedirs(save_dir, exist_ok=True)
    print('save_dir : ', save_dir)

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    os.makedirs(os.path.join(save_dir, 'results'), exist_ok=True)

    n_splits = 5
    skf = StratifiedKFold(n_splits=n_splits)

    patience = 10
    accumulation_steps = 2
    oof_pred = None

    labels = [dataset.encode_multi_class(mask, gender, age) for mask, gender, age in zip(
        dataset.mask_labels, dataset.gender_labels, dataset.age_labels)]

    # K-Fold Cross Validationκ³? ??Ό?κ²? Train, Valid Indexλ₯? ??±?©??€.
    for i, (train_idx, valid_idx) in enumerate(skf.split(dataset.image_paths, labels)):
        train_loader, val_loader = getDataloader(
            dataset, train_idx, valid_idx, args.batch_size, num_workers)

        best_val_acc = 0
        best_val_loss = np.inf
        counter = 0

        # -- model
        model_module = getattr(import_module(
            "model"), args.model)  # default: BaseModel
        model = model_module(
            num_classes=num_classes
        ).to(device)

        # -- loss & metric
        criterion = create_criterion(args.criterion)

        # -- optimizer
        opt_module = getattr(import_module("torch.optim"),
                             args.optimizer)  # default: SGD
        optimizer = opt_module(
            filter(lambda p: p.requires_grad, model.parameters()),
            lr=args.lr,
            weight_decay=5e-4
        )

        # -- scheduler
        scheduler = StepLR(optimizer, args.lr_decay_step, gamma=0.5)

        # -- logging
        for epoch in range(args.epochs):
            # train loop
            model.train()
            loss_value = 0
            matches = 0
            for idx, train_batch in enumerate(train_loader):
                inputs, (mask_labels, gender_labels, age_labels) = train_batch
                inputs = inputs.to(device)

                mask_labels = mask_labels.to(device)
                gender_labels = gender_labels.to(device)
                age_labels = age_labels.to(device)

                outs = model(inputs)
                (mask_outs, gender_outs, age_outs) = torch.split(
                    outs, [3, 2, 3], dim=1)
                mask_loss = criterion(mask_outs, mask_labels)
                gender_loss = criterion(gender_outs, gender_labels)
                age_loss = criterion(age_outs, age_labels)

                mask_preds = torch.argmax(mask_outs, dim=1)
                gender_preds = torch.argmax(gender_outs, dim=1)
                age_preds = torch.argmax(age_outs, dim=1)

                loss = mask_loss + gender_loss + 1.5 * age_loss

                loss.backward()

                preds, labels = dataset.encode_multi_class(
                    mask_preds, gender_preds, age_preds), dataset.encode_multi_class(mask_labels, gender_labels, age_labels)

                # -- Gradient Accumulation
                if (idx+1) % accumulation_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()

                loss_value += loss.item()
                matches += (preds == labels).sum().item()
                if (idx + 1) % train_log_interval == 0:
                    train_loss = loss_value / train_log_interval
                    train_acc = matches / args.batch_size / train_log_interval
                    current_lr = scheduler.get_last_lr()
                    print(
                        f"Epoch[{epoch}/{args.epochs}]({idx + 1}/{len(train_loader)}) || "
                        f"training loss {train_loss:4.4} || training accuracy {train_acc:4.2%} || lr {current_lr}"
                    )

                    loss_value = 0
                    matches = 0
            # wandb ??΅ ?¨κ³μ? Loss, Accuracy λ‘κ·Έ ????₯
            wandb.log({
                "train_loss": train_loss,
                "train_acc": train_acc
            })

            scheduler.step()

            # val loop
            with torch.no_grad():
                print("Calculating validation results...")
                model.eval()
                val_loss_items = []
                val_acc_items = []

                mask_loss_items = []
                gender_loss_items = []
                age_loss_items = []

                mask_val_acc_items = []
                gender_val_acc_items = []
                age_val_acc_items = []
                figure = None
                for val_batch in val_loader:
                    inputs, (mask_labels, gender_labels,
                             age_labels) = val_batch
                    inputs = inputs.to(device)
                    mask_labels = mask_labels.to(device)
                    gender_labels = gender_labels.to(device)
                    age_labels = age_labels.to(device)

                    outs = model(inputs)
                    (mask_outs, gender_outs, age_outs) = torch.split(
                        outs, [3, 2, 3], dim=1)

                    mask_loss_item = criterion(mask_outs, mask_labels).item()
                    gender_loss_item = criterion(
                        gender_outs, gender_labels).item()
                    age_loss_item = criterion(age_outs, age_labels).item()
                    loss_item = mask_loss_item + gender_loss_item + age_loss_item

                    mask_preds = torch.argmax(mask_outs, dim=1)
                    gender_preds = torch.argmax(gender_outs, dim=1)
                    age_preds = torch.argmax(age_outs, dim=1)

                    preds, labels = dataset.encode_multi_class(
                        mask_preds, gender_preds, age_preds), dataset.encode_multi_class(mask_labels, gender_labels, age_labels)

                    acc_item = (labels == preds).sum().item()
                    mask_val_acc = (mask_labels == mask_preds).sum().item()
                    gender_val_acc = (
                        gender_labels == gender_preds).sum().item()
                    age_val_acc = (age_labels == age_preds).sum().item()

                    val_loss_items.append(loss_item)
                    val_acc_items.append(acc_item)

                    mask_val_acc_items.append(mask_val_acc)
                    gender_val_acc_items.append(gender_val_acc)
                    age_val_acc_items.append(age_val_acc)

                    mask_loss_items.append(mask_loss_item)
                    gender_loss_items.append(gender_loss_item)
                    age_loss_items.append(age_loss_item)

                    if figure is None:
                        inputs_np = torch.clone(inputs).detach(
                        ).cpu().permute(0, 2, 3, 1).numpy()
                        inputs_np = dataset_module.denormalize_image(
                            inputs_np, dataset.mean, dataset.std)
                        figure = grid_image(
                            inputs_np, labels, preds, n=16, shuffle=args.dataset != "MaskSplitByProfileDataset")

                # mask, genderm age (?κ·? loss,acc κ³μ°)

                mask_val_loss = np.sum(mask_loss_items) / len(val_loader)
                gender_val_loss = np.sum(gender_loss_items) / len(val_loader)
                age_val_loss = np.sum(age_loss_items) / len(val_loader)

                mask_val_acc = np.sum(mask_val_acc_items) / len(valid_idx)
                gender_val_acc = np.sum(gender_val_acc_items) / len(valid_idx)
                age_val_acc = np.sum(age_val_acc_items) / len(valid_idx)

                # one class loss, acc κ³μ°

                val_loss = np.sum(val_loss_items) / len(val_loader)
                val_acc = np.sum(val_acc_items) / len(valid_idx)

                # Callback1: validation accuracyκ°? ?₯?? ?λ‘? λͺ¨λΈ? ????₯?©??€.
                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                if val_acc > best_val_acc:
                    print("New best model for val accuracy! saving the model..")
#                     torch.save(model.state_dict(), f"{save_dir}/results/{epoch:03}_accuracy_{val_acc:4.2%}.ckpt")
                    torch.save(model.state_dict(), f"{save_dir}/best.pth")
                    best_val_acc = val_acc
                    counter = 0
                else:
                    print(f"Not Update val accuracy... counter : {counter}")
                    counter += 1

                torch.save(model.state_dict(), f"{save_dir}/last.pth")
                print(
                    f"[Val] acc : {val_acc:4.2%}, loss: {val_loss:4.2} || "
                    f"best acc : {best_val_acc:4.2%}, best loss: {best_val_loss:4.2}"
                )
                print()
                # wandb κ²?μ¦? ?¨κ³μ? Loss, Accuracy λ‘κ·Έ ????₯
                wandb.log({
                    "valid_loss": val_loss,
                    "valid_acc": val_acc,
                    "mask_loss": mask_val_loss,
                    "mask_acc": mask_val_acc,
                    "gender_loss": gender_val_loss,
                    "gender_acc": gender_val_acc,
                    "age_loss": age_val_loss,
                    "age_acc": age_val_acc
                })
                # Callback2: patience ?? ?? ?±?₯ ?₯??΄ ?? κ²½μ° ??΅? μ’λ£??΅??€.
                if counter > patience:
                    print("Early Stopping...")
                    break

        # κ°? fold?? ??±? λͺ¨λΈ? ?¬?©?΄ Test ?°?΄?°λ₯? ?μΈ‘ν©??€.
        all_predictions = []
        with torch.no_grad():
            for images in test_loader:
                images = images.to(device)

                # Test Time Augmentation
                pred = model(images) / 2  # ?λ³? ?΄λ―Έμ??λ₯? ?μΈ‘νκ³?
                # horizontal_flip?Όλ‘? ?€μ§μ΄ ?μΈ‘ν©??€.
                pred += model(torch.flip(images, dims=(-1,))) / 2
                all_predictions.extend(pred.cpu().numpy())

            fold_pred = np.array(all_predictions)

        # ?λ₯? κ°μΌλ‘? ??λΈμ μ§ν?κΈ? ?λ¬Έμ 'k'κ°λ‘ ???΄μ€λ?€.
        if oof_pred is None:
            oof_pred = fold_pred / n_splits
        else:
            oof_pred += fold_pred / n_splits

    submission['ans'] = np.argmax(oof_pred, axis=1)
    submission.to_csv(os.path.join(
        args.output_dir, 'skf_ml_submission.csv'), index=False)
    print('test inference is done!')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Data and model checkpoints directories
    parser.add_argument('--seed', type=int, default=42,
                        help='random seed (default: 42)')
    parser.add_argument('--epochs', type=int, default=100,
                        help='number of epochs to train (default: 1)')
    parser.add_argument('--dataset', type=str, default='MaskBaseDataset',
                        help='dataset augmentation type (default: MaskBaseDataset)')
    parser.add_argument('--augmentation', type=str, default='BaseAugmentation',
                        help='data augmentation type (default: BaseAugmentation)')
    parser.add_argument("--resize", nargs="+", type=int,
                        default=[128, 96], help='resize size for image when training')
    parser.add_argument('--batch_size', type=int, default=64,
                        help='input batch size for training (default: 64)')
    parser.add_argument('--valid_batch_size', type=int, default=1000,
                        help='input batch size for validing (default: 1000)')
    parser.add_argument('--model', type=str, default='BaseModel',
                        help='model type (default: BaseModel)')
    parser.add_argument('--optimizer', type=str, default='SGD',
                        help='optimizer type (default: SGD)')
    parser.add_argument('--lr', type=float, default=1e-3,
                        help='learning rate (default: 1e-3)')
    parser.add_argument('--val_ratio', type=float, default=0.2,
                        help='ratio for validaton (default: 0.2)')
    parser.add_argument('--criterion', type=str, default='cross_entropy',
                        help='criterion type (default: cross_entropy)')
    parser.add_argument('--lr_decay_step', type=int, default=20,
                        help='learning rate scheduler deacy step (default: 20)')
    parser.add_argument('--log_interval', type=int, default=20,
                        help='how many batches to wait before logging training status')
    parser.add_argument('--name', default='exp',
                        help='model save at {SM_MODEL_DIR}/{name}')

    # Container environment
    parser.add_argument('--data_dir', type=str, default=os.environ.get(
        'SM_CHANNEL_TRAIN', '/opt/ml/input/data/train/images'))
    parser.add_argument('--model_dir', type=str,
                        default=os.environ.get('SM_MODEL_DIR', './model'))
    parser.add_argument('--output_dir', type=str,
                        default=os.environ.get('SM_MODEL_DIR', './output'))

    args = parser.parse_args()

    wandb.init(project='wandb_test')
    wandb.config.update(args)
    wandb.run.name = 'k-fold'
    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir
    output_dir = args.output_dir

    os.makedirs(model_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    train(data_dir, model_dir, args)
