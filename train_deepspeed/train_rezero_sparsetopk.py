import sys
sys.path.append('../')
import torch
from tqdm import tqdm
import datetime
from torch.utils.data import DataLoader, random_split
import deepspeed
from common.dataset import GPTXDatasetV2
from common.arg import ModelConfig
from model.pipeline import ReZroSparseTopkGPTPipe
from transformers import BertTokenizer
from ds_util import get_argument_parser
from transformers import get_cosine_schedule_with_warmup
from deepspeed.pipe import PipelineModule
import wandb
import os
import json
import logging
from model.n_transformer import LayerNorm

def pretrain():
    """Main Train
    1) setup model, optimizer and lr_schedule
    2) set dataset
    3) train the model
    """
    args = get_arguments()

    torch.manual_seed(9)
    deepspeed.runtime.utils.set_random_seed(9)

    deepspeed.init_distributed(dist_backend='nccl')
    args.local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(-1) # local rank passed from distributed launcher

    config = ModelConfig(config_path=args.config).get_config()

    tokenizer = BertTokenizer(vocab_file=config.vocab_path, do_lower_case=False)

    dataset = GPTXDatasetV2(tokenizer, config.max_seq_len, config.data_path)

    wandb.init(project="rezero_sparsetopk_gpt")

    train_dataloader, eval_dataloader = build_dataloaders(config, dataset, train_test_split=0.1)

    config.max_train_step = len(train_dataloader) * config.epoch
    config.max_eval_step = len(eval_dataloader)

    model, optimizer, lr_scheduler = setup_model_and_optimizer(config)

    model,optimizer, _, lr_scheduler = deepspeed.initialize(
        model=model,
        optimizer=optimizer,
        args=args,
        lr_scheduler=lr_scheduler,
        dist_init_required=False,
        training_data=train_dataloader,
        eval_dataloader = eval_dataloader
    )


    train(config=config,
          model=model,
          optimizer=optimizer,
          lr_scheduler=lr_scheduler,
          train_dataloader=train_dataloader,
          eval_dataloader=eval_dataloader)

    evaluate(config, model)
def train(config,
          model,
          optimizer,
          lr_scheduler,
          train_dataloader,
          eval_dataloader):

    # Set train mode
    model.train()

    for _ in range(config.max_train_step):
        lm_logit, loss = model.train_batch()
        wandb.log({'train': {'loss': loss.item(), 'perplexity': torch.exp(loss)}})

    train_result = {"loss": loss.item(), "ppl": torch.exp(loss)}

    return train_result

def evaluate(config, model):
    model.eval()

    with torch.no_grad():
        for _ in range(config.max_eval_step):
            loss = model.eval_batch(return_logit=False)
            wandb.log({'eval': {'loss': loss.item(), 'perplexity': torch.exp(loss)}})

    eval_result = {"loss": loss.item(), "ppl": torch.exp(loss)}
    model.train()
    return eval_result

def setup_model_and_optimizer(config):
    """"""
    model = get_model(config)
    optimizer, model_params = get_optimizer(config, model)
    lr_scheduler = get_learning_rate_scheduler(optimizer=optimizer, config=config)

    return model, optimizer, lr_scheduler

def get_model(config):
    model = ReZroSparseTopkGPTPipe(vocab_size= config.vocab_size,
                     dim = config.dim,
                     depth = config.depth,
                     n_head= config.n_head,
                     max_seq_len= config.max_seq_len)
    model = model.to_layer()
    model = PipelineModule(layers=model,
                           num_stages=config.num_stages)
    return model

def get_model_params(config, model):
    weight_decay_params = {"params": [], 'weight_decay': config['weight_decay']}
    no_weight_decay_params = {"params": [], 'weight_decay': 0.0}
    for module in model:
        if isinstance(module,LayerNorm) or config.weight_decay==0.0:
            no_weight_decay_params["params"].extend(
                [p for p in list(module._parameters.values()) if p is not None]
            )
        else:
            weight_decay_params["params"]\
                .extend([p for n, p in list(module._parameters.items()) if p is not None and n != "bias"])
            no_weight_decay_params["params"]\
                .extend([p for n, p in list(module._parameters.items()) if p is not None and n == "bias"])
    if config.weight_decay==0.0:
        return [no_weight_decay_params]

    return weight_decay_params, no_weight_decay_params

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
    return optimizer,model_params

def get_learning_rate_scheduler(optimizer, config):
    num_iter = config.max_train_step
    warmup_num_iter= num_iter * config['warmup_iter']
    lr_scheduler = get_cosine_schedule_with_warmup(optimizer=optimizer,
                                                   num_warmup_steps=warmup_num_iter,
                                                   num_training_steps=num_iter)
    return lr_scheduler

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

if __name__ == '__main__':
    pretrain()