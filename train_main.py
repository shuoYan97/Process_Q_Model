import pandas as pd
import numpy as np
import json
import random
import math
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import IterableDataset,Dataset
from datasets import load_dataset, load_from_disk
from accelerate import Accelerator
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
from value_model import AutoModelForCausalLMWithValueHead
import torch
import sys, os
from tqdm import tqdm
import torch.nn.functional as F
import torch.nn as nn
import re
import math
import argparse


def seed_everything(seed=0):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


class PRMTrainer(Trainer):
    def __init__(self, model=None,
                 args=None,
                 data_collator=None,
                 train_dataset=None,
                 eval_dataset=None,
                 tokenizer=None,
                 model_init=None,
                 compute_metrics=None,
                 callbacks=None,
                 optimizers=(None, None),
                 preprocess_logits_for_metrics=None, ):
        super().__init__(model=model,
                         args=args,
                         data_collator=data_collator,
                         train_dataset=train_dataset,
                         eval_dataset=eval_dataset,
                         tokenizer=tokenizer,
                         model_init=model_init,
                         compute_metrics=compute_metrics,
                         callbacks=callbacks,
                         optimizers=optimizers,
                         preprocess_logits_for_metrics=preprocess_logits_for_metrics, )
        self.loss_type = args.loss_type
        if self.loss_type == 'nce' or self.loss_type == 'orm':
            self.loss_fn = nn.BCELoss(reduction='none')
        elif self.loss_type=='mse':
            self.loss_fn = nn.MSELoss(reduction='none')

    def ranking_loss(self,rewards,labels,has_neg):
        pos_rewards_exp = torch.where(labels == 1, (rewards).exp(), 0)
        neg_rewards_exp = torch.where(labels == 0, (rewards+args.zeta).exp(), 0).flip(dims=[-1])
        neg_reward_sum = neg_rewards_exp.sum(-1)

        pos_rewards_cumsum = torch.cat([torch.zeros(rewards.shape[0], 1, device=rewards.device).exp(), pos_rewards_exp],
                                       dim=1).cumsum(-1)[:, :-1]
        pos_rewards_cumsum = torch.cat([torch.zeros(rewards.shape[0], 1, device=rewards.device), pos_rewards_cumsum],
                                       dim=-1)

        reward_exp_cur = torch.where(labels == 1, pos_rewards_exp, 1)
        reward_exp_cur = torch.cat([torch.zeros(rewards.shape[0], 1, device=rewards.device).exp(), reward_exp_cur], dim=-1)

        loss = -torch.log(reward_exp_cur / (reward_exp_cur + pos_rewards_cumsum + neg_reward_sum[..., None] + 1e-5))

        labels = torch.cat([has_neg[..., None], labels], dim=-1)
        loss = (torch.where(labels == 1, loss, 0).sum(-1) / torch.where(labels == 1, 1, 0).sum(-1)).mean()
        return loss


    def compute_loss(self, model, inputs, return_outputs=False):

        _,_,rewards = model(input_ids=inputs['input_ids'],attention_mask=inputs['attention_mask'])

        if self.loss_type=='nce':
            rewards = rewards.gather(dim=-1, index=inputs['special_tokens'])
            rewards = rewards.sigmoid()
            loss = (self.loss_fn(rewards, torch.where(inputs['step_labels']!=-100,inputs['step_labels'],0).bfloat16()) * (inputs['step_labels']!=-100)).sum()/(inputs['step_labels']!=-100).sum()
        elif self.loss_type=='mse':
            rewards = rewards.gather(dim=-1, index=inputs['special_tokens'])
            rewards = rewards.sigmoid()
            loss = (self.loss_fn(rewards,
                                 torch.where(inputs['step_labels'] != -100, inputs['step_labels'], 0).bfloat16()) * (
                                inputs['step_labels'] != -100)).sum() / (inputs['step_labels'] != -100).sum()
        elif self.loss_type=='orm':
            rewards = rewards.gather(dim=-1, index=inputs['orm_tokens'][...,None])
            rewards = rewards.sigmoid()
            loss = self.loss_fn(rewards.squeeze(1),1-inputs['has_neg'].bfloat16()).mean()
        elif self.loss_type=='rank':
            rewards = rewards.gather(dim=-1, index=inputs['special_tokens'])
            loss = self.ranking_loss(rewards,inputs['step_labels'],inputs['has_neg'])

        return loss

def instruction_format(s):
    return f'[INST] {s} [/INST]'

def generate_dataset(prm_token,tokenizer):
    ds = load_from_disk(args.dataset_path)['train']
    ds = [d for d in ds]
    queries = []
    longer_queries = []
    longest_queries = []
    statistic = [0,0,0]
    for d in ds:
        input_text = d['input']
        steps = re.split('Step \d+:', input_text)
        steps = [s for s in steps if s.strip() != '']
        if len(steps) == 1:
            continue
        question = steps[0]
        steps = [f'Step {i + 1}: ' + step.strip().replace('ки', '').strip() for i, step in enumerate(steps[1:]) if
                 step.strip() != '']
        label_steps = re.split('Step \d+:', d['label'])
        label_steps = [s.strip() for s in label_steps[1:]]
        try:
            for s in label_steps:
                assert s[-1] in ['+', '-'], (label_steps)
        except:
            continue
        step_labels = [1 if l[-1] == '+' else 0 for l in label_steps]
        try:
            assert len(steps) == len(step_labels)
        except:
            continue
        queries.append({
            "query": instruction_format(question),
            "answer": f" {prm_token}\n".join(steps) + f" {prm_token}",
            "labels": step_labels,  # + [outcome_label],
        })
        ids = tokenizer.encode(queries[-1]['query'] + queries[-1]['answer'])
        if len(ids) > 512 and len(ids)<=1024:
            longer_queries.append(queries.pop())
        elif len(ids) > 1024:
            longest_queries.append(queries.pop())

        #[392777, 49233, 2543] , len split:512, 1024

    if accelerator.is_local_main_process:
        print(f'Data Examples:\n{queries[0]}\n{queries[-1]}')
        print(f'Dataset Length:{len(queries)}')
        print(statistic)

    return queries,longer_queries,longest_queries


class TrainDataset(Dataset):
    def __init__(self, dataset1, dataset2, dataset3):
        self.iteration_1 = math.ceil(len(dataset1)/64)
        self.iteration_2 = math.ceil(len(dataset2)/24)
        self.iteration_3 = math.ceil(len(dataset3)/8)
        self.dataset = []
        for i in range(self.iteration_3):
            self.dataset.append(dataset3[i*8:(i + 1) *8])
        for i in range(self.iteration_2):
            self.dataset.append(dataset2[i*24:(i + 1)*24])
        for i in range(self.iteration_1):  # 更多操作在这里完成
            self.dataset.append(dataset1[i*64:(i+1)*64])

    def __getitem__(self,index):
        return self.dataset[index]

    def __len__(self):
        return self.iteration_1+self.iteration_2+self.iteration_3

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-path", type=str, default="/nobackup/hf-dataset-download/Math-Shepherd")
    parser.add_argument("--model-path", type=str, default="/nobackup/hf-model/deepseek-math-7b-base")
    parser.add_argument("--save-path", type=str, default="/nobackup/prm_checkpoints/neg-zeta-16")
    parser.add_argument("--zeta", type=int, default=4)
    parser.add_argument("--loss-type", type=str, default='rank',
                        choices=['rank', 'orm', 'mse', 'bce'])
    args = parser.parse_args()
    print(args)

    seed_everything(0)
    accelerator = Accelerator()
    prm_token = '[PRM]'

    tokenizer = AutoTokenizer.from_pretrained(args.model_path,trust_remote_code=True)
    if not tokenizer.pad_token:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(args.model_path,
                                                 torch_dtype=torch.bfloat16,use_flash_attention_2=True,trust_remote_code=True)
    tokenizer.add_special_tokens({'additional_special_tokens':[prm_token]})
    prm_token_id = tokenizer.encode(prm_token, add_special_tokens=False)[-1]
    dataset1,dataset2,dataset3 = generate_dataset(prm_token, tokenizer)
    dataset = TrainDataset(dataset1,dataset2,dataset3)

    model.resize_token_embeddings(len(tokenizer))
    reward_model = AutoModelForCausalLMWithValueHead(model)

    def data_collator(example, tokenizer=tokenizer):
        inputs = []
        special_ids = []
        step_labels = []
        orm_tokens,orm_labels = [],[]
        has_neg = []
        template = '{query}\n{answer}'
        example = example[0]
        for d in example:
            input_ids = tokenizer.encode(template.format(query=d['query'],answer=d['answer']),
                                          add_special_tokens=False)
            inputs.append(torch.tensor(input_ids))

            cur_special_ids = []
            for ii,id in enumerate(input_ids):
                if id==prm_token_id:
                    cur_special_ids.append(ii)
            assert len(cur_special_ids)==len(d['labels'])
            special_ids.append(torch.tensor(cur_special_ids))
            step_labels.append(torch.tensor(d['labels']))
            orm_tokens.append(cur_special_ids[-1])
            has_neg.append(1 if 0 in d['labels'] else 0)

        inputs = pad_sequence(inputs, padding_value=tokenizer.pad_token_id, batch_first=True)
        attention_mask = (inputs!=tokenizer.pad_token_id)
        special_ids = pad_sequence(special_ids, padding_value=0, batch_first=True)
        step_labels = pad_sequence(step_labels, padding_value=-100, batch_first=True)

        return {
            'input_ids': inputs.int(),
            'attention_mask': attention_mask.int(),
            'special_tokens':special_ids,
            'step_labels':step_labels,
            'orm_tokens':torch.tensor(orm_tokens),
            'has_neg':torch.tensor(has_neg)
        }


    deepspeed_config = json.load(open('accelerate_configs/deepspeed_3.json'))
    deepspeed_config["scheduler"]["params"] = {
        "warmup_min_lr": 0,
        "warmup_max_lr": 'auto',
        "warmup_num_steps": 'auto',
        "total_num_steps": 'auto'
    }

    training_args = TrainingArguments(
        output_dir=args.save_path,
        overwrite_output_dir=True,

        optim="adamw_torch",
        learning_rate=1e-6, # 2e-6 for 8 GPUs

        lr_scheduler_type="cosine",
        # warmup_steps = 150,
        warmup_ratio=0.1,
        gradient_checkpointing=True,
        num_train_epochs=2,
        gradient_accumulation_steps=4, #4 for 8 GPUs
        per_device_train_batch_size=1,
        logging_steps=1,
        save_strategy="epoch",
        report_to="none",
        remove_unused_columns=False,
        bf16=True,
        fp16_backend="auto",
        disable_tqdm=False,
        save_safetensors=False,
        # group_by_length = True,
        deepspeed=deepspeed_config,
        # sharded_ddp="zero_dp_2",
    )

    trainer = PRMTrainer(
        reward_model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=dataset,
        data_collator=data_collator
    )

    trainer.train()