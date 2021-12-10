import torch
from tqdm import tqdm
import datetime
from torch.utils.data import DataLoader, random_split
import deepspeed
from common.dataset import GPTXDataset
from common.arg import ModelConfig
from model.pipeline import GPTXPipe
from transformers import BertTokenizer
from ds_util import get_argument_parser
from transformers import get_cosine_schedule_with_warmup
from deepspeed.pipe import PipelineModule
import wandb
import os
import json
import logging

def pretrain(arg):
    """Main Train
    1) setup model, optimizer and lr_schedule
    2) set dataset
    3) train the model
    """
    args = get_arguments()

    config = ModelConfig(config_path=args.config).get_config()

    tokenizer = BertTokenizer(vocab_file=config.vocab_path, do_lower_case=False)

    dataset = GPTXDataset(tokenizer, config.max_seq_len, config.data_path)

    logging.basicConfig(filename=f'{config.log_dir}/{config.model_name}-{datetime.now().date()}.log', level=logging.INFO)
    wandb.init(project="gpt-x")

    train_dataloader, eval_dataloader = build_dataloaders(config, dataset, train_test_split=0.1)

    config['max_train_step'] = len(train_dataloader) * config.epoch

    model, optimizer, lr_scheduler = setup_model_and_optimizer(arg)

    train(config=config,
          model=model,
          optimizer=optimizer,
          lr_scheduler=lr_scheduler,
          train_dataloader=train_dataloader,
          eval_dataloader=eval_dataloader)

def train(config,
          model,
          optimizer,
          lr_scheduler,
          train_dataloader,
          eval_dataloader):
    # Variables
    losses = {}
    perplexity = 0.0

    # Set train mode
    model.train()

    # Train
    train_progress_iter = tqdm(iterable=enumerate(train_dataloader),
                       total=config['max_train_step'],
                       desc='GPTX Train Iterator',
                       bar_format='{l_bar}{bar:10}{r_bar}')

    for step, batch in train_progress_iter:
        inputs, labels = batch
        lm_logit, loss = model(inputs, labels)

        step_ppl = torch.exp(loss)
        perplexity += step_ppl
        model.backward(loss)
        model.step()





def evaluate(config, model, dataloader):
    pass
def setup_model_and_optimizer(arg, config):
    """"""
    model = get_model(config)
    optimizer = get_optimizer(config, model)

def get_model(config):
    model = GPTXPipe(vocab_size= config.vocab_size,
                    dim = config.dim,
                    depth = config.depth,
                    head_num= config.n_head,
                    max_seq_len= config.max_seq_len)
    model = model.to_layer()
    return model
def get_model_params(model):
    """
    no weight
    """
    model_params = {"params": []}
    for module in model:
        model_params['params'].extend([p for n, p in list(module._parameters.items()) if p is not None and n != "bias"])
    return [model_params]

def get_optimizer(config, model):
    model_params = get_model_params(model)
    if config['optimizer']['type']=='cpu_adam':
        from deepspeed.ops.adam import DeepSpeedCPUAdam
        optimizer = DeepSpeedCPUAdam(model_params,
                                              **config.optimizer['params'])
    elif config['optimizer']['type']=='adam':
        from deepspeed.ops.adam import FusedAdam as Adam
        optimizer = Adam(model_params,
                         **config.optimizer['params'])
    return optimizer
def get_learning_rate_scheduler(optimizer, config):
    num_iter = config['max_train_step']
    warmup_num_iter= num_iter * config['warmup_iter']
    lr_scheduler = get_cosine_schedule_with_warmup(optimizer=optimizer,
                                                   num_warmup_steps=warmup_num_iter,
                                                   num_training_steps=num_iter
                                                   )
def build_dataloaders(config, dataset, train_test_split=0.1, train_shuffle=True, eval_shuffle=True):
    dataset_len = len(dataset)
    eval_len = int(dataset_len * train_test_split)
    train_len = dataset_len - eval_len
    train_dataset, eval_dataset = random_split(dataset, (train_len, eval_len))
    train_loader = DataLoader(train_dataset, batch_size=config.batch_size, shuffle=train_shuffle)
    eval_loader = DataLoader(eval_dataset, batch_size=config.batch_size, shuffle=eval_shuffle)
    logging.info(f'''train_dataloader size: {len(train_loader.dataset)} | shuffle: {train_shuffle}
                     eval_dataloader  size: {len(eval_loader.dataset)} | shuffle: {eval_shuffle}''')

    return train_loader, eval_loader

def get_arguments():
    parser = get_argument_parser()
    # Include DeepSpeed configuration arguments
    parser = deepspeed.add_config_arguments(parser)
    args = parser.parse_args()
    # no cuda mode is not supported
    args.no_cuda = False

    return args