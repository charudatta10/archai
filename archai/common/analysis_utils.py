# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import argparse
from typing import Dict, List, Type, Iterator, Tuple
import glob
import os
import pathlib
from collections import OrderedDict
import yaml
from inspect import getsourcefile
import re
import math as ma

from runstats import Statistics

import matplotlib
matplotlib.use('Agg')
import seaborn as sns
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd


from archai.common import utils
from archai.common.ordereddict_logger import OrderedDictLogger
import re


def epoch_nodes(node:OrderedDict, path=[])->Iterator[Tuple[List[str], OrderedDict]]:
    """Search nodes recursively for nodes named 'epochs' and return them along with their paths"""
    for k, v in node.items():
        if k == 'epochs' and isinstance(v, OrderedDict) and len(v) and '0' in v:
            yield path, v
        elif isinstance(v, OrderedDict): # make recursive call
            for p, en in epoch_nodes(v, path=path+[k]):
                yield p, en

def fix_yaml(filepath:str):
    # fix yaml construction recursion error because of bad lines

    yaml = pathlib.Path(filepath).read_text()

    bad_lines = [
            r'get: !!python/object/apply:builtins.getattr',
            r'- *id001',
            r' - get'
        ]
    # form pattern by joining str literals after escape by whitespace /s
    # Note: don't use re.escape as it cannot be used in re.sub
    pattern = r'\s+'.join([re.escape(l) for l in bad_lines])
    fixed_yaml = re.sub(pattern, '', yaml)

    if yaml != fixed_yaml:
        backup = pathlib.Path(filepath+'.original.yaml')
        assert not backup.exists(), f'Backup file {backup} should not exist'
        backup.write_text(yaml)
        pathlib.Path(filepath).write_text(fixed_yaml)
        print(f'Yaml at {filepath} was fixed')

def remove_seed_part(log_key:str)->str:
    # regex identifies seed123, seed123.4, seed_123, seed_123.4
    # pattern is 'seed' followed by optional '_' followed by int or float number
    pat = r'seed\_?([0-9]*[.])?[0-9]+'
    return re.sub(pat, '', log_key)

def group_multi_runs(logs:Dict[str, OrderedDict])->Dict[str, List[OrderedDict]]:
    result:Dict[str, List[OrderedDict]] = {}
    for log_key, log in logs.items():
        seed_less_key = remove_seed_part(log_key)
        if seed_less_key in result:
            result[seed_less_key].append(log)
        else:
            result[seed_less_key] = [log]
    return result

def collect_epoch_nodes(grouped_logs:Dict[str, List[OrderedDict]])->Dict[str, Dict[str, List[OrderedDict]]]:
    """Make list of epoch nodes in same path in each of the logs if collate=True else
       its just list of epoch nodes with jobdir and path as the key."""
    collated:Dict[str, Dict[str, List[OrderedDict]]] = {}

    for log_key, logs in grouped_logs.items():
        collated_logs:Dict[str, List[OrderedDict]] = {}
        for log in logs:
            for path, epoch_node in epoch_nodes(log):
                # for each path get the list where we can put epoch node
                path_key = '/'.join(path)
                if not path_key in collated_logs:
                    collated_logs[path_key] = []
                v = collated_logs[path_key]
                v.append(epoch_node)
        collated[log_key] = collated_logs
    return collated

class EpochStats:
    def __init__(self) -> None:
        self.start_lr = Statistics()
        self.end_lr = Statistics()
        self.train_fold = FoldStats()
        self.val_fold = FoldStats()

    def update(self, epoch_node:OrderedDict)->None:
        self.start_lr.push(epoch_node['start_lr'])

        if 'train' in epoch_node:
            self.end_lr.push(epoch_node['train']['end_lr'])
            self.train_fold.update(epoch_node['train'])
        if 'val' in epoch_node:
            self.val_fold.update(epoch_node['val'])

class FoldStats:
    def __init__(self) -> None:
        self.top1 = Statistics()
        self.top5 = Statistics()
        self.duration = Statistics()
        self.step_time = Statistics()

    def update(self, fold_node:OrderedDict)->None:
        self.top1.push(fold_node['top1'])
        self.top5.push(fold_node['top5'])
        if 'duration' in fold_node:
            self.duration.push(fold_node['duration'])
        if 'step_time' in fold_node:
            self.step_time.push(fold_node['step_time'])

def stat2str(stat:Statistics)->str:
    if len(stat) == 0:
        return '-'
    s = f'{stat.mean():.4f}'
    if len(stat)>1:
        s += f'<sup> &pm; {stat.stddev():.4f}</sup>'
    return s

def get_epoch_stats(node_path:str, logs_epochs_nodes:List[OrderedDict])->List[EpochStats]:
    epoch_stats = []

    for epochs_node in logs_epochs_nodes:
        for epoch_num, epoch_node in epochs_node.items():
            if not str.isnumeric(epoch_num): # each epoch key must be numeric
                continue
            epoch_num = int(epoch_num)
            if epoch_num >= len(epoch_stats):
                epoch_stats.append(EpochStats())
            epoch_stat = epoch_stats[epoch_num]
            epoch_stat.update(epoch_node)

    return epoch_stats

def get_valid_filename(s):
    s = str(s).strip().replace(' ', '-')
    return re.sub(r'(?u)[^-\w.]', '-', s)

def get_summary_text(log_key:str, out_dir:str, node_path:str, epoch_stats:List[EpochStats], seed_runs:int)->str:
    lines = ['','']

    lines.append(f'## Run: {log_key}\n')
    lines.append(f'### Metric Type: {node_path}\n')

    lines.append(f'Number of epochs: {len(epoch_stats)}\n')
    lines.append(f'Number of seeds: {seed_runs}\n')

    lines.append('\n')
    plot_filename = get_valid_filename(log_key + ':' + node_path)+'.png'
    plot_filepath = os.path.join(out_dir, plot_filename)
    plot_epochs(epoch_stats, plot_filepath)
    lines.append('')

    train_duration = Statistics()
    for epoch_stat in epoch_stats:
        train_duration += epoch_stat.train_fold.duration

    lines.append(f'![]({plot_filename})')

    lines.append(f'Train epoch time: {stat2str(train_duration)}')
    lines.append('')
    milestones = [0, 5, 30, 100, 200, 600, 1500]
    for milestone in milestones:
        if len(epoch_stats) >= milestone and len(epoch_stats[milestone-1].val_fold.top1)>0:
            lines.append(f'{stat2str(epoch_stats[milestone-1].val_fold.top1)} val top1 @ {milestone} epochs\n')
    # last epoch
    if not len(epoch_stats) in milestones:
        # find last epoch with valid stats
        last_epoch = len(epoch_stats)-1
        while last_epoch>=0 and len(epoch_stats[last_epoch].val_fold.top1)==0:
            last_epoch -= 1
        if last_epoch >=0:
            lines.append(f'{stat2str(epoch_stats[last_epoch].val_fold.top1)} val top1 @ {len(epoch_stats)} epochs [Last]\n')
        else:
            lines.append(f'[Last] No epoch with valid val stats found!')

    return '\n'.join(lines)

def get_details_text(log_key:str, out_dir:str, node_path:str, epoch_stats:List[EpochStats], seed_runs:int)->str:
    lines = ['','']

    lines.append(f'## Run: {log_key}\n')
    lines.append(f'### Metric Type: {node_path}\n')

    lines.append(f'Number of seeds: {seed_runs}\n')


    lines.append('|Epoch   |Val Top1   |Val Top5   |Train  Top1 |Train Top5   |Train Duration   |Val Duration   |Train Step Time     |Val Step Time   |StartLR   |EndLR   |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|---|')

    for i, epoch_stat in enumerate(epoch_stats):
        line = '|'
        line += str(i) + '|'
        line += stat2str(epoch_stat.val_fold.top1) + '|'
        line += stat2str(epoch_stat.val_fold.top5) + '|'
        line += stat2str(epoch_stat.train_fold.top1) + '|'
        line += stat2str(epoch_stat.train_fold.top5) + '|'
        line += stat2str(epoch_stat.train_fold.duration) + '|'
        line += stat2str(epoch_stat.val_fold.duration) + '|'
        line += stat2str(epoch_stat.train_fold.step_time) + '|'
        line += stat2str(epoch_stat.val_fold.step_time) + '|'
        line += stat2str(epoch_stat.start_lr) + '|'
        line += stat2str(epoch_stat.end_lr) + '|'

        lines.append(line)

    return '\n'.join(lines)

def plot_epochs(epoch_stats:List[EpochStats], filepath:str):
    plt.ioff()
    plt.clf()
    fig, ax = plt.subplots()
    clrs = sns.color_palette("husl", 5)
    with sns.axes_style("darkgrid"):
        metrics = []
        val_top1_means = [es.val_fold.top1.mean() if len(es.val_fold.top1)>0 else np.nan for es in epoch_stats]
        val_top1_std = [es.val_fold.top1.stddev() if len(es.val_fold.top1)>1 else np.nan for es in epoch_stats]
        val_top1_min = [es.val_fold.top1.minimum() if len(es.val_fold.top1)>0 else np.nan for es in epoch_stats]
        val_top1_max = [es.val_fold.top1.maximum() if len(es.val_fold.top1)>0 else np.nan for es in epoch_stats]
        metrics.append((val_top1_means, val_top1_std, 'val_top1', val_top1_min, val_top1_max))

        val_top5_means = [es.val_fold.top5.mean() if len(es.val_fold.top5)>0 else np.nan for es in epoch_stats]
        val_top5_std = [es.val_fold.top5.stddev() if len(es.val_fold.top5)>1 else np.nan for es in epoch_stats]
        val_top5_min = [es.val_fold.top5.minimum() if len(es.val_fold.top5)>0 else np.nan for es in epoch_stats]
        val_top5_max = [es.val_fold.top5.maximum() if len(es.val_fold.top5)>0 else np.nan for es in epoch_stats]
        metrics.append((val_top5_means, val_top5_std, 'val_top5', val_top5_min, val_top5_max))

        train_top1_means = [es.train_fold.top1.mean() if len(es.train_fold.top1)>0 else np.nan for es in epoch_stats]
        train_top1_std = [es.train_fold.top1.stddev() if len(es.train_fold.top1)>1 else np.nan for es in epoch_stats]
        train_top1_min = [es.train_fold.top1.minimum() if len(es.train_fold.top1)>0 else np.nan for es in epoch_stats]
        train_top1_max = [es.train_fold.top1.maximum() if len(es.train_fold.top1)>0 else np.nan for es in epoch_stats]
        metrics.append((train_top1_means, train_top1_std, 'train_top1', train_top1_min, train_top1_max))

        train_top5_means = [es.train_fold.top5.mean() if len(es.train_fold.top5)>0 else np.nan for es in epoch_stats]
        train_top5_std = [es.train_fold.top5.stddev() if len(es.train_fold.top5)>1 else np.nan for es in epoch_stats]
        train_top5_min = [es.train_fold.top1.minimum() if len(es.train_fold.top5)>0 else np.nan for es in epoch_stats]
        train_top5_max = [es.train_fold.top1.maximum() if len(es.train_fold.top5)>0 else np.nan for es in epoch_stats]
        metrics.append((train_top5_means, train_top5_std, 'train_top5', train_top5_min, train_top5_max))

        for i, metric in enumerate(metrics):
            ax.plot(range(len(metric[0])), metric[0], label=metric[2], c=clrs[i])
            ax.fill_between(range(len(metric[0])), np.subtract(metric[0], metric[1]),
                            np.add(metric[0], metric[1]),
                            alpha=0.5, facecolor=clrs[i])
            ax.fill_between(range(len(metric[0])), metric[3],
                            metric[4],
                            alpha=0.1, facecolor=clrs[i])
        ax.set_xlabel('Epoch')
        ax.set_ylabel('Accuracy')
        ax.set_title('Accuracy Metrics')

        ax.legend()
        ax.grid('on')

        # add more ticks
        #ax.set_xticks(np.arange(max([len(m) for m in metrics])))
        # remove tick marks
        # ax.xaxis.set_tick_params(size=0)
        # ax.yaxis.set_tick_params(size=0)

        # change the color of the top and right spines to opaque gray
        # ax.spines['right'].set_color((.8,.8,.8))
        # ax.spines['top'].set_color((.8,.8,.8))

        # tweak the axis labels
        xlab = ax.xaxis.get_label()
        ylab = ax.yaxis.get_label()

        xlab.set_style('italic')
        xlab.set_size(10)
        ylab.set_style('italic')
        ylab.set_size(10)

        # tweak the title
        ttl = ax.title
        ttl.set_weight('bold')
    plt.savefig(filepath)
    plt.close()


def write_report(template_filename:str, **kwargs)->None:
    source_file = getsourcefile(lambda:0)
    script_dir = os.path.dirname(os.path.abspath(source_file))
    template = pathlib.Path(os.path.join(script_dir, template_filename)).read_text()
    report = template.format(**kwargs)
    outfilepath = os.path.join(kwargs['out_dir'], template_filename)
    with open(outfilepath, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f'report written to: {outfilepath}')


def find_valid_log(subdir:str)->str:
    # originally log should be in base folder of eval or search
    logs_filepath_og = os.path.join(str(subdir), 'log.yaml')
    if os.path.isfile(logs_filepath_og):
        return logs_filepath_og
    else:
        # look in the 'dist' folder for any yaml file
        dist_folder = os.path.join(str(subdir), 'dist')

        # sometimes some job may have been run multiple times
        # resulting in multiple log files. here we take the 
        # largest one as that has the most chance of being complete.

        # get list of files
        list_of_files = filter(lambda x: os.path.isfile(os.path.join(dist_folder, x)),
                        os.listdir(dist_folder))
                
        # find the largest yaml file
        size = -ma.inf
        largest_log_file = ''
        list_of_yamls = [f for f in list_of_files if f.endswith(".yaml")]
        for f in list_of_yamls:
            this_size = os.path.getsize(os.path.join(dist_folder, f))
            if this_size > size:
                size = this_size
                largest_log_file = f
        assert largest_log_file != ''
        return os.path.join(dist_folder, largest_log_file) 

        


def parse_a_job(job_dir:str)->Dict:
     if job_dir.is_dir():

        storage = {}
        for subdir in job_dir.iterdir():
            if not subdir.is_dir():
                continue
            # currently we expect that each job was ExperimentRunner job which should have
            # _search or _eval folders
            if subdir.stem.endswith('_search'):
                sub_job = 'search'
            elif subdir.stem.endswith('_eval'):
                sub_job = 'eval'
            else:
                raise RuntimeError(f'Sub directory "{subdir}" in job "{job_dir}" must '
                                'end with either _search or _eval which '
                                'should be the case if ExperimentRunner was used.')

            logs_filepath = find_valid_log(subdir)
            # if no valid logfile found, ignore this job as it probably 
            # didn't finish or errored out or is yet to run
            if not logs_filepath:
                continue

            config_used_filepath = os.path.join(subdir, 'config_used.yaml')

            if os.path.isfile(logs_filepath):
                fix_yaml(logs_filepath)
                key = job_dir.name + subdir.name + ':' + sub_job
                # parse log
                with open(logs_filepath, 'r') as f:
                    data = yaml.load(f, Loader=yaml.Loader)                    
                # parse config used
                with open(config_used_filepath, 'r') as f:
                    confs = yaml.load(f, Loader=yaml.Loader)
                storage[key] = (data, confs)
            
        return storage