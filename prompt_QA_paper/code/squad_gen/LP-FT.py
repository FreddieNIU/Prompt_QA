import collections
import json

import numpy as np
from nltk.tokenize import word_tokenize
from torch.utils import data
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter   
from tqdm import tqdm
from transformers import AdamW
from transformers import BartForConditionalGeneration
from transformers import T5ForConditionalGeneration

from config import *
from MyBART import *
from MyT5 import *

class SquadDataset(torch.utils.data.Dataset):
    def __init__(self, question_encodings, answer_encodings):
        self.question_encodings = question_encodings
        self.answer_encodings = answer_encodings

    def __getitem__(self, idx):
        return {key: torch.tensor(val[idx]) for key, val in self.question_encodings.items()}, \
               {key: torch.tensor(val[idx]) for key, val in self.answer_encodings.items()}

    def __len__(self):
        return len(self.question_encodings.input_ids)


def read_dataset(path,have_template=True):
    contexts = []
    questions = []
    answers = []

    qa_dataset = json.load(open(path, 'r', encoding='utf8'))
    for qa in qa_dataset:
        if have_template:
            template = qa['template']
            context = qa['context'] + str([template[i] for i in range(len(template))]) 
        else:
            context = qa['context']
        answer_start = qa['answer'][0]
        answer_end = qa['answer'][1]
        answer_text = context[answer_start: answer_end]
        question = 'question: ' + qa['question']

        contexts.append(context)
        questions.append(question)
        answers.append(answer_text)

    return contexts, questions, answers


def compute_f1(gold_toks, pred_toks):
    common = collections.Counter(gold_toks) & collections.Counter(pred_toks)
    num_same = sum(common.values())
    if len(gold_toks) == 0 or len(pred_toks) == 0:
        return int(gold_toks == pred_toks), 0, 0

    if num_same == 0:
        return 0, 0, 0

    precision = 1.0 * num_same / len(pred_toks)
    recall = 1.0 * num_same / len(gold_toks)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1, precision, recall


def train(model, num_epoch=1, SAVE=True, lr=1e-5):
    model.train()
    train_loader = DataLoader(train_dataset, batch_size=config.train_batch_size, shuffle=True)

    optim = AdamW(model.parameters(), lr=lr)

    cur_best_f1 = 0
    batch_index = 0
    writer = SummaryWriter(config.summary_writer_path)


    for epoch in range(num_epoch):
        for question_batch, answer_batch in tqdm(train_loader):
            batch_index += 1

            question_input_ids = question_batch['input_ids'].to(config.device)
            question_attention_mask = question_batch['attention_mask'].to(config.device)
            labels = answer_batch['input_ids'].to(config.device)
            outputs = model(question_input_ids,
                            attention_mask=question_attention_mask,
                            labels=labels, output_hidden_states=True)
            loss = outputs.loss
            loss.backward()
            
            writer.add_scalar('LPFT_{}_loss'.format(config.model_name), loss, epoch)    
            
            if batch_index % config.batch_accum == 0:
                optim.step()
                optim.zero_grad()
                batch_index = 0

            torch.cuda.empty_cache()

        dev_f1, dev_precision, dev_recall, dev_exact_match = test(model=model, config=config, tokenizer=tokenizer,
            test_loader=DataLoader(dev_dataset, batch_size=config.test_batch_size, shuffle=False))
        print('Epoch:', epoch, 'dev f1:', dev_f1, 'precision:', dev_precision, 'recall:', dev_recall, 'exact_match:', dev_exact_match)
        if dev_f1 > cur_best_f1:
            cur_best_f1 = dev_f1
            if SAVE:
                torch.save(model, config.model_save_path)
    linear_params = {}
    for name, param in model.lm_head.named_parameters():
        linear_params[name] = param
    
    return cur_best_f1 ,linear_params


def test(test_loader, model, config, tokenizer):
    model.eval()
    with torch.no_grad():
        f1_list = []
        precision_list = []
        recall_list = []
        exact_match_list = []

        for question_batch, answer_batch in tqdm(test_loader):
            question_input_ids = question_batch['input_ids'].to(config.device)
            gold_answer_ids = answer_batch['input_ids'].to(config.device)
            outputs = model.generate(question_input_ids)

            pred_answers = [tokenizer.decode(outputs[i], skip_special_tokens=True) for i in range(len(outputs))]
            gold_answers = [tokenizer.decode(gold_answer_ids[i], skip_special_tokens=True) for i in range(len(gold_answer_ids))]

            for pred_answer, gold_answer in zip(pred_answers, gold_answers):
                pred_toks = []
                [pred_toks.append(toks) for toks in word_tokenize(pred_answer)]
                gold_toks = []
                [gold_toks.append(toks) for toks in word_tokenize(gold_answer)]
                f1, precision, recall = compute_f1(gold_toks, pred_toks)
                exact_match = 1 if gold_answer == pred_answer else 0

                f1_list.append(f1)
                precision_list.append(precision)
                recall_list.append(recall)
                exact_match_list.append(exact_match)

        f1_test = np.mean(f1_list)
        precision_test = np.mean(precision_list)
        recall_test = np.mean(recall_list)
        exact_match_test = np.mean(exact_match_list)
        return f1_test, precision_test, recall_test, exact_match_test


if __name__ == '__main__':
    configs = [
                # BartConfig(),
                BartPromptConfig(),
            #    BartLargeConfig(), 
               # T5SmallConfig(), 
               # T5Config(), 
                # T5LargeConfig()
              ]

    for config in configs:
        print('model:', config.model)
        print('train data: ', config.train_data_path)
        print('dev data: ', config.dev_data_path)
        print('test data: ', config.test_data_path)
        print('model saved: ', config.model_save_path)



        train_contexts, train_questions, train_answers = read_dataset(config.train_data_path, have_template=config.have_template)
        dev_contexts, dev_questions, dev_answers = read_dataset(config.dev_data_path, have_template=config.have_template)
        test_contexts, test_questions, test_answers = read_dataset(config.test_data_path, have_template=config.have_template)

        tokenizer = config.tokenizer

        train_question_encodings = tokenizer(train_questions, train_contexts, truncation=True, padding=True)
        train_answer_encodings = tokenizer(train_answers, truncation=True, padding=True)
        dev_question_encodings = tokenizer(dev_questions, dev_contexts, truncation=True, padding=True)
        dev_answer_encodings = tokenizer(dev_answers, truncation=True, padding=True)
        test_question_encodings = tokenizer(test_questions, test_contexts, truncation=True, padding=True)
        test_answer_encodings = tokenizer(test_answers, truncation=True, padding=True)

        train_dataset = SquadDataset(train_question_encodings, train_answer_encodings)
        dev_dataset = SquadDataset(dev_question_encodings, dev_answer_encodings)
        test_dataset = SquadDataset(test_question_encodings, test_answer_encodings)

        # training
        if 'bart' in config.model:
            LP_model = MyBartForConditionalGeneration_LP.from_pretrained(config.model)
        else:
            LP_model = MyT5ForConditionalGeneration_LP.from_pretrained(config.model)
  
        LP_model.to(config.device)
        
        print('training')
        previous_BART = list(LP_model.named_parameters())[1:]
        previous_BART_tensors = []
        for i in range(len(previous_BART)):
            previous_BART_tensors.append(previous_BART[i][1].clone())
        previous_head = list(LP_model.lm_head.named_parameters())[0][1].clone()
        
        best_dev_f1, linear_params = train(LP_model, num_epoch=2, SAVE=False, lr=1e-6)
        
        Flag = 0
        for i in range(len(previous_BART_tensors)):
            a = (previous_BART_tensors[i]== list(LP_model.named_parameters())[1:][i][1])
            if False in a:
                Flag = 1
                print('BART/T5 updated')
                break
        if Flag == 0:
            print('BART/T5 not updated')
        a = (previous_head == list(LP_model.lm_head.named_parameters())[0][1])
        if False in a:
            print('Head updated')
        else:
            print('Head not updated')  
        
        # head_param_init = {}
        # for name, param in LP_model.lm_head.named_parameters():
        #     head_param_init[name] = param
                
        
        head_param_init =  LP_model.lm_head.state_dict()
        print("LP Head parameter: ", head_param_init)


        if 'bart' in config.model:
            LPFT_model = MyBartForConditionalGeneration_LPFT.from_pretrained(config.model)

        else:
            LPFT_model = MyT5ForConditionalGeneration_LPFT.from_pretrained(config.model)
  
        
        print('LPFT head parameter: ', list(LPFT_model.lm_head.named_parameters()))
        # for name, param in LPFT_model.lm_head.named_parameters():
        #     param = head_param_init[name]
        LPFT_model.lm_head.load_state_dict(head_param_init)
        print('LPFT head initialized: ', list(LPFT_model.lm_head.named_parameters()))
        
        LPFT_model.to(config.device)
        best_dev_f1, linear_params = train(LPFT_model, num_epoch=config.num_epoch, SAVE=True, lr=config.lr)



        # for name, param in model.model.named_parameters():
        #     if 'shared' not in name:
        #         print("BART",name, param) 
        #         break
        # for name, param in model.lm_head.named_parameters():
        #     print("Linear Head", name, param) 
        # print(linear_params)

        # testing
        print('testing')
        model = torch.load(config.model_save_path)

        train_f1, train_precision, train_recall, train_exact_match = test(model=model, config=config, tokenizer=tokenizer,
            test_loader=DataLoader(train_dataset, batch_size=config.test_batch_size, shuffle=False))
        print('train f1:', train_f1, 'precision:', train_precision, 'recall:', train_recall, 'exact_match:', train_exact_match)

        dev_f1, dev_precision, dev_recall, dev_exact_match = test(model=model, config=config, tokenizer=tokenizer,
            test_loader=DataLoader(dev_dataset, batch_size=config.test_batch_size, shuffle=False))
        print('dev f1:', dev_f1, 'precision:', dev_precision, 'recall:', dev_recall, 'exact_match:', dev_exact_match)

        test_f1, test_precision, test_recall, test_exact_match = test(model=model, config=config, tokenizer=tokenizer,
            test_loader=DataLoader(test_dataset, batch_size=config.test_batch_size, shuffle=False))
        print('test f1:', test_f1, 'precision:', test_precision, 'recall:', test_recall, 'exact_match:', test_exact_match)

