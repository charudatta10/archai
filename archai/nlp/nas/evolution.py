# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Evolutionary search-related classes and methods.
"""

import copy
import os
import pickle
import random
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import plotly.graph_objects as go

from archai.nlp.models.model_loader import load_config, load_model_formula, load_model_from_config
from archai.nlp.nas.nas_utils.constraints import (measure_inference_latency,
                                                  measure_parameters,
                                                  measure_peak_memory)
from archai.nlp.nas.nas_utils.converter import Converter
from archai.nlp.nas.nas_utils.dispatcher import (create_ground_truth_jobs,
                                                 create_pareto_jobs)
from archai.nlp.nas.nas_utils.pareto_front import find_pareto_points
from archai.nlp.nas.nas_utils.plotter import plot_2d_pareto, plot_3d_pareto


class Evolution:
    """Implements the evolutionary search (Genetic Algorithm).

    """

    def __init__(self,
                 results_path: str,
                 model_type: Optional[str] = 'mem_transformer',
                 model_config: Optional[Dict[str, Any]] = None,
                 population_size: Optional[int] = 100,
                 parent_size: Optional[int] = 20,
                 mutation_size: Optional[int] = 40,
                 mutation_prob: Optional[float] = 0.3,
                 crossover_size: Optional[int] = 40,
                 crossover_prob: Optional[float] = 0.5,
                 n_iter: Optional[int] = 10,
                 use_quantization: Optional[bool] = False,
                 param_constraint_lower: Optional[int] = 5e6,
                 param_constraint_upper: Optional[int] = 12e6,
                 latency_constraint_upper: Optional[float] = None,
                 n_threads: Optional[int] = 1,
                 latency_repeat: Optional[int] = 5,
                 **choices) -> None:
        """Initializes attributes.

        Args:
            results_path: Path to the folder that will save the results.
            model_type: Type of model.
            model_config: Model configuration to override default configuration.
            population_size: Size of the population.
            parent_size: Size of the parent genes.
            mutation_size: Size of the mutated genes.
            mutation_prob: Probability of mutation.
            crossover_size: Size of the crossovered genes.
            crossover_prob: Probability of crossover.
            n_iter: Number of search iterations.
            use_quantization: Whether should use quantization or not.
            param_constraint_lower: Any candidate below this will get rejected.
            param_constraint_upper: Any candidate above this will get rejected.
            latency_constraint_upper: Any model which has higher latency is rejected.
            n_threads: Number of inference threads.
            latency_repeat: Number of latency measurements.
            choices: Additional keyword arguments that represent hyperparameters choices.

        """

        self.results_path = results_path
        self.n_iter = n_iter
        self.use_quantization = use_quantization

        # Sizes and probabilities of the search space
        self.population_size = population_size
        self.parent_size = parent_size
        self.mutation_size = mutation_size
        self.mutation_prob = mutation_prob
        self.crossover_size = crossover_size
        self.crossover_prob = crossover_prob
        assert self.population_size == self.parent_size + self.mutation_size + self.crossover_size

        # Total number of parameters and latency constraints
        self.param_constraint_lower = param_constraint_lower
        self.param_constraint_upper = param_constraint_upper
        self.latency_constraint_upper = latency_constraint_upper
        
        # Number of threads and runs for latency measurement
        self.n_threads = n_threads
        self.latency_repeat = latency_repeat
        
        # Model's default and search configurations
        self.model_type = model_type
        self.model_config = load_config(model_type, config_type='default')
        self.model_config_search = load_config(model_type, config_type='search')

        # Overrides default configuration with inputted ones
        self.model_config.update((k, v) for k, v in model_config.items() 
                                 if k in self.model_config.keys() and v is not None)

        # Prevents non-available keys from being used during search
        # Also, overrides default search choices with inputted ones
        for k, v in choices.items():
            if k in self.model_config_search.keys() and v is not None:
                self.model_config_search[k]['value'] = v

        # Converts between genes and configurations
        self.converter = Converter(**self.model_config_search)
        self.allowed_genes = self.converter.get_allowed_genes()
        self.gene_size = len(self.allowed_genes)

        with open(os.path.join(self.results_path, 'converter.pkl'), 'wb') as f:
            pickle.dump(self.converter, f)
        
        # Pareto-frontier points
        self.pareto = {'population': [],
                       'params': [],
                       'total_params': [],
                       'latencies': [],
                       'memories': []}

        # All evaluated points
        self.all_population = []
        self.all_params = []
        self.all_total_params = []
        self.all_latencies = []
        self.all_memories = []

        # Counter for the number of genes occurences
        self.counts = Counter()

        # Performs a quick profiling over the search space
        # to find the biggest architecture measurements
        self._profile()

    def _profile(self) -> None:
        """Profiles the search space.

        """

        def _profile_model(config: Dict[str, Any]) -> Tuple[int, int, float, float]:
            model_config = copy.deepcopy(self.model_config)
            model_config.update(config)

            model = load_model_from_config(self.model_type, model_config)

            params = measure_parameters(model, ['non_embedding'])
            total_params =  measure_parameters(model, ['total'])
            latency = measure_inference_latency(model, use_quantization=self.use_quantization)
            peak_memory = measure_peak_memory(model, use_quantization=self.use_quantization)

            return params, total_params, latency, peak_memory

        # Largest model    
        max_gene = [self.allowed_genes[k][-1] for k in range(self.gene_size)]
        max_config = self.converter.gene_to_config(max_gene)

        self.max_params, \
        self.max_total_params, \
        self.max_latency, \
        self.max_peak_memory = _profile_model(max_config)

        print(f'''Largest model in this space has: 
                {max_config}
                {self.max_params} decoder params
                {self.max_total_params} total params
                {self.max_latency:.4f}s latency
                {self.max_peak_memory:.4f}MB memory''')

        # Smallest model
        min_gene = [self.allowed_genes[k][0] for k in range(self.gene_size)]
        min_config = self.converter.gene_to_config(min_gene)

        self.min_params, \
        self.min_total_params, \
        self.min_latency, \
        self.min_peak_memory = _profile_model(min_config)
        
        print(f'''Smallest model in this space has: 
                {min_config}
                {self.min_params} decoder params
                {self.min_total_params} total params
                {self.min_latency:.4f}s latency
                {self.min_peak_memory:.4f}MB memory''')

    def _check_constraints(self, gene: List[Any]) -> bool:
        """Checks whether gene fulfill constraints or not.

        Args:
            gene: Gene.

        Returns:
            (bool): Whether gene has fulfilled constraints or not.

        """

        # Converts gene to configuration
        config = self.converter.gene_to_config(gene)

        # Loads model from current configuration
        model_config = copy.deepcopy(self.model_config)
        model_config.update(config)

        # Checks if model passes number of parameter constraints via analytical means since it is fast
        total_params_analytical = load_model_formula(self.model_type)(model_config)['total']

        if total_params_analytical < self.param_constraint_lower:
            print(f'Invalid gene: {gene} has {total_params_analytical/1e6:.4f}M < {self.param_constraint_lower/1e6:.4f}M parameters')
            return False
    
        if total_params_analytical > self.param_constraint_upper:
            print(f'Invalid gene: {gene} has {total_params_analytical/1e6:.4f}M > {self.param_constraint_upper/1e6:.4f}M parameters')
            return False

        # If the analytical check is valid, model is finally created
        model = load_model_from_config(self.model_type, model_config)

        # Checks the latency constraint
        if self.latency_constraint_upper is not None:
            latency = measure_inference_latency(model,
                                                use_quantization=self.use_quantization,
                                                n_threads=self.n_threads,
                                                n_trials=self.latency_repeat)
            
            if latency > self.latency_constraint_upper:
                print(f'Invalid gene: {gene} has {latency}s > {self.latency_constraint_upper}s latency')
                return False

        return True

    def _calculate_constraints(self, genes: List[List[Any]]) -> Tuple[List[int], List[int], List[float], List[float]]:
        """Calculates decoder parameters, total parameters, memory and latency.

        Args:
            genes: List of genes.

        Returns:
            (Tuple[List[int], List[int], List[float], List[float]]): Decoder parameters,
                total parameters, latencies and memories. 

        """

        configs = []
        for gene in genes:
            configs.append(self.converter.gene_to_config(gene))
        
        params = []
        total_params = []
        latencies = []
        memories = []

        for config in configs:
            model_config = copy.deepcopy(self.model_config)
            model_config.update(config)
            model = load_model_from_config(self.model_type, model_config)
            
            # Decoder parameters
            d_params = measure_parameters(model, ['non_embedding'])
            params.append(d_params)

            # Total parameters
            t_params = measure_parameters(model, ['total'])
            total_params.append(t_params)

            # Latency
            latency = measure_inference_latency(model,
                                                use_quantization=self.use_quantization,
                                                n_threads=self.n_threads,
                                                n_trials=self.latency_repeat)
            latencies.append(latency)

            # Memory
            memory = measure_peak_memory(model, use_quantization=self.use_quantization)
            memories.append(memory)
            
        # Sanity checking
        assert len(params) == len(latencies)
        assert len(params) == len(memories)
        assert len(params) == len(total_params)
        
        return params, total_params, latencies, memories

    def _update_pareto_front(self, is_decreasing: Optional[bool] = True) -> None:
        """Updates the Pareto-frontier of the evolutionary search.

        Args:
            is_decreasing: Whether Pareto-frontier is decreasing or not.
            
        """

        self.pareto = defaultdict(list)

        # Pareto over decoder params, latency, memory since
        # higher decoder params is better for performance and lower memory and latency are better
        # Note we convert decoder params to a decreasing quantity since the pareto
        # finding function needs all of them to be either decreasing or increasing
        xs = np.array(max(self.all_params)) - np.array(self.all_params).reshape(-1, 1)
        ys = np.array(self.all_latencies).reshape(-1, 1)
        zs = np.array(self.all_memories).reshape(-1, 1)

        points = np.concatenate((xs, ys, zs), axis=1)
        p_inds = find_pareto_points(points, is_decreasing=is_decreasing)

        assert points.shape[0] == len(self.all_population)
        assert points.shape[0] == len(self.all_params)
        assert points.shape[0] == len(self.all_total_params)
        assert points.shape[0] == len(self.all_latencies)
        assert points.shape[0] == len(self.all_memories)
        
        self.pareto['population'] = [self.all_population[i] for i in p_inds]
        self.pareto['params'] = [self.all_params[i] for i in p_inds]
        self.pareto['total_params'] = [self.all_total_params[i] for i in p_inds]
        self.pareto['latencies'] = [self.all_latencies[i] for i in p_inds]
        self.pareto['memories'] = [self.all_memories[i] for i in p_inds]
            
        print(f'Pareto-frontier points: {len(self.pareto["population"])}')

    def _calculate_weighted_count(self) -> np.array:
        """Assigns a weight to each member of the Pareto-frontier such that it is inversely 
            proportional to the number of times it has already been in the working set population.
            
        This is used to select parents from the Pareto-frontier to prevent
        the same architectures from always being in the parent pool.
        
        Returns:
            (np.array): Weighted count.

        """

        pareto_counts = []

        for gene in self.pareto['population']:
            key = self.converter.gene_to_key(gene)
            pareto_counts.append(self.counts[key])

        counts_max = max(pareto_counts)
        counts_min = min(pareto_counts)
        counts_range = counts_max if (counts_max == counts_min) else (counts_max - counts_min)

        # Scales between [0, 1] to avoid numerical issues
        scaled_counts = [(count - counts_min) / counts_range for count in pareto_counts]
        count_weights = [1.0 / (scaled_count + 1) for scaled_count in scaled_counts]
        count_weights = np.asarray(count_weights) / np.sum(count_weights)

        assert count_weights.size == len(self.pareto['population'])

        return count_weights

    def _mutation(self, gene: List[Any]) -> List[Any]:
        """Performs mutation over a single gene.

        Args:
            gene: Gene.

        Returns:
            (List[Any]): Mutated gene.

        """

        mutated_gene = []

        for k in range(self.gene_size):
            if np.random.uniform() < self.mutation_prob:
                mutated_gene.append(random.choices(self.allowed_genes[k])[0])
            else:
                mutated_gene.append(gene[k])

        return mutated_gene

    def _crossover(self, genes: List[List[Any]]) -> List[List[Any]]:
        """Performs the crossover between genes.

        Args:
            genes: List of genes.

        Returns:
            (List[List[Any]]): Crossovered genes.

        """
                
        crossovered_gene = []

        for k in range(self.gene_size):
            if np.random.uniform() < self.crossover_prob:
                crossovered_gene.append(genes[0][k])
            else:
                crossovered_gene.append(genes[1][k])

        return crossovered_gene

    def _is_seen_before(self, gene: List[Any]) -> bool:
        """Checks whether gene has already been seen during search.

        Args:
            gene: Gene to be checked.

        Returns:
            (bool): Whether gene has already been seen during search.

        """

        key = self.converter.gene_to_key(gene)

        if key in self.counts.keys():
            return True
        
        return False

    def _update_gene_count(self, gene: List[Any]) -> None:
        """Updates the number of a single repeated gene.

        Args:
            gene: Current gene.

        """

        key = self.converter.gene_to_key(gene)

        # Important to add as a dictionary because it
        # prevents Counter from counting the characters in the string
        self.counts.update({key: 1})

    def _update_population_count(self, population: List[List[Any]]) -> None:
        """Updates the number of repeated genes in the population.

        Args:
            population: Current population.

        """

        for gene in population:
            self._update_gene_count(gene)

    def plot_search_state(self,
                          iteration: Optional[int] = None,
                          parents: Optional[Dict[str, Any]] = None) -> None:
        """Plots the state of search at every iteration.

        Args:
            iteration: Current iteration number.
            parents: Dictionary with parent samples.

        """

        all_configs = [self.converter.gene_to_config(gene) for gene in self.all_population]
        all_params = np.asarray(self.all_params)
        all_total_params = np.asarray(self.all_total_params)
        all_latencies = np.asarray(self.all_latencies)
        all_memories = np.asarray(self.all_memories)

        pareto_configs = [self.converter.gene_to_config(gene) for gene in self.pareto['population']]
        pareto_params = np.asarray(self.pareto['params'])
        pareto_total_params = np.asarray(self.pareto['total_params'])
        pareto_latencies = np.asarray(self.pareto['latencies'])
        pareto_memories = np.asarray(self.pareto['memories'])

        if parents:
            parents_configs = [self.converter.gene_to_config(gene) for gene in parents['population']]
            parents_params = np.asarray(parents['params'])
            parents_total_params = np.asarray(parents['total_params'])
            parents_latencies = np.asarray(parents['latencies'])
            parents_memories = np.asarray(parents['memories'])

        # 2D plot: number of decoder parameters x latencies 
        visited_dict = {'x': all_params, 'y': all_latencies, 'config': all_configs}
        pareto_dict = {'x': pareto_params, 'y': pareto_latencies, 'config': pareto_configs}
        parents_dict = {'x': parents_params, 'y': parents_latencies, 'config': parents_configs} if parents else None
        output_path = os.path.join(self.results_path, f'decoder_params_vs_latency_iter_{iteration}')

        plot_2d_pareto(visited_dict,
                       pareto_dict,
                       parents_dict,
                       hover_template='Decoder params: %{x:d}' + '<br>Latency (s): %{y:.4f}<br>' + '%{text}',
                       title_text=f'Decoder params vs. Latency (s) at Iteration {iteration}',
                       xaxis_title='Decoder params',
                       yaxis_title='Latency (s)',
                       output_path=output_path)

        # 2D plot: number of total parameters x latencies 
        visited_dict = {'x': all_total_params, 'y': all_latencies, 'config': all_configs}
        pareto_dict = {'x': pareto_total_params, 'y': pareto_latencies, 'config': pareto_configs}
        parents_dict = {'x': parents_total_params, 'y': parents_latencies, 'config': parents_configs} if parents else None
        output_path = os.path.join(self.results_path, f'total_params_vs_latency_iter_{iteration}')

        plot_2d_pareto(visited_dict,
                       pareto_dict,
                       parents_dict,
                       hover_template='Total params: %{x:d}' + '<br>Latency (s): %{y:.4f}<br>' + '%{text}',
                       title_text=f'Total params vs. Latency (s) at Iteration {iteration}',
                       xaxis_title='Total params',
                       yaxis_title='Latency (s)',
                       output_path=output_path)

        # 2D plot: number of decoder parameters x memories 
        visited_dict = {'x': all_params, 'y': all_memories, 'config': all_configs}
        pareto_dict = {'x': pareto_params, 'y': pareto_memories, 'config': pareto_configs}
        parents_dict = {'x': parents_params, 'y': parents_memories, 'config': parents_configs} if parents else None
        output_path = os.path.join(self.results_path, f'decoder_params_vs_memory_iter_{iteration}')

        plot_2d_pareto(visited_dict,
                       pareto_dict,
                       parents_dict,
                       hover_template='Decoder params: %{x:d}' + '<br>Memory (MB): %{y:.4f}<br>' + '%{text}',
                       title_text=f'Decoder params vs. Memory (MB) at Iteration {iteration}',
                       xaxis_title='Decoder params',
                       yaxis_title='Memory (MB)',
                       output_path=output_path)
        
        # 2D plot: number of total parameters x memories 
        visited_dict = {'x': all_total_params, 'y': all_memories, 'config': all_configs}
        pareto_dict = {'x': pareto_total_params, 'y': pareto_memories, 'config': pareto_configs}
        parents_dict = {'x': parents_total_params, 'y': parents_memories, 'config': parents_configs} if parents else None
        output_path = os.path.join(self.results_path, f'total_params_vs_memory_iter_{iteration}')

        plot_2d_pareto(visited_dict,
                       pareto_dict,
                       parents_dict,
                       hover_template='Total params: %{x:d}' + '<br>Memory (MB): %{y:.4f}<br>' + '%{text}',
                       title_text=f'Total params vs. Memory (MB) at Iteration {iteration}',
                       xaxis_title='Total params',
                       yaxis_title='Memory (MB)',
                       output_path=output_path)

        # 3D plot: number of decoder parameters x latencies x memories 
        visited_dict = {'x': all_params, 'y': all_memories, 'z': all_latencies, 'config': all_configs}
        pareto_dict = {'x': pareto_params, 'y': pareto_memories, 'z': pareto_latencies, 'config': pareto_configs}
        parents_dict = {'x': parents_params, 'y': parents_memories, 'z': parents_latencies, 'config': parents_configs} if parents else None
        output_path = os.path.join(self.results_path, f'decoder_params_vs_memory_vs_latency_iter_{iteration}')

        plot_3d_pareto(visited_dict,
                       pareto_dict,
                       parents_dict,
                       hover_template='Decoder params: %{x:d}' + '<br>Memory (MB): %{y:.4f}<br>' + 'Latency (s): %{z:.4f}<br>' + '%{text}',
                       title_text=f'Decoder params vs. Memory (MB) vs. Latency (s) at Iteration {iteration}',
                       xaxis_title='Decoder params',
                       yaxis_title='Memory (MB)',
                       zaxis_title='Latency (s)',
                       output_path=output_path)

        # 3D plot: number of total parameters x latencies x memories 
        visited_dict = {'x': all_total_params, 'y': all_memories, 'z': all_latencies, 'config': all_configs}
        pareto_dict = {'x': pareto_total_params, 'y': pareto_memories, 'z': pareto_latencies, 'config': pareto_configs}
        parents_dict = {'x': parents_total_params, 'y': parents_memories, 'z': parents_latencies, 'config': parents_configs} if parents else None
        output_path = os.path.join(self.results_path, f'total_params_vs_memory_vs_latency_iter_{iteration}')

        plot_3d_pareto(visited_dict,
                       pareto_dict,
                       parents_dict,
                       hover_template='Total params: %{x:d}' + '<br>Memory (MB): %{y:.4f}<br>' + 'Latency (s): %{z:.4f}<br>' + '%{text}',
                       title_text=f'Total params vs. Memory (MB) vs. Latency (s) at Iteration {iteration}',
                       xaxis_title='Total params',
                       yaxis_title='Memory (MB)',
                       zaxis_title='Latency (s)',
                       output_path=output_path)

    def sample_random_population(self, n_samples: int) -> List[List[Any]]:
        """Samples a random population.

        Args:
            n_samples: Number of genes to be sampled.

        Returns:
            (List[List[Any]]): Randomly sampled population.

        """

        population = []

        i = 0
        while i < n_samples:
            sampled_gene = []

            for k in range(self.gene_size):
                sampled_gene.append(random.choices(self.allowed_genes[k])[0])

            if self._check_constraints(sampled_gene):
                population.append(sampled_gene)
                i += 1
                print(f'Valid architectures: {i}/{n_samples}')

        return population

    def search(self) -> None:
        """Performs the actual search.

        """

        # Samples the initial population    
        population = self.sample_random_population(self.population_size)

        self.all_population = population
        self._update_population_count(population)

        logs = {'population': [],
                'params': [],
                'total_params': [],
                'latencies': [],
                'memories': [],
                'parents': [],
                'pareto': []}

        parents_params = []
        parents_total_params = []
        parents_latencies = []
        parents_memories = []

        for i in range(self.n_iter):
            idx = 0 if i == 0 else self.parent_size
            print(f'Iteration {i+1}/{self.n_iter}')

            # Calculates decoder parameters, total parameters, latencies and memories
            population_params_unseen, \
            population_total_params_unseen, \
            population_latencies_unseen, \
            population_memories_unseen = self._calculate_constraints(population[idx:])

            population_params = parents_params + population_params_unseen
            population_total_params = parents_total_params + population_total_params_unseen
            population_latencies = parents_latencies + population_latencies_unseen
            population_memories = parents_memories + population_memories_unseen

            assert len(population_params) == self.population_size
            assert len(population_total_params) == self.population_size
            assert len(population_latencies) == self.population_size
            assert len(population_memories) == self.population_size
            
            self.all_params += population_params_unseen
            self.all_total_params += population_total_params_unseen
            self.all_latencies += population_latencies_unseen
            self.all_memories += population_memories_unseen

            print(f'Visited population points: {len(self.all_population)}')

            self._update_pareto_front(is_decreasing=True)

            # Selects parents for the next iteration from the current estimate
            # of the Pareto-frontier while giving more weight to newer parents
            weights = self._calculate_weighted_count()
            selected_idx = np.random.choice(len(self.pareto['population']),
                                            size=self.parent_size,
                                            p=weights)

            parents_population = [self.pareto['population'][m] for m in selected_idx]
            parents_params = [self.pareto['params'][m] for m in selected_idx]
            parents_total_params = [self.pareto['total_params'][m] for m in selected_idx]
            parents_latencies = [self.pareto['latencies'][m] for m in selected_idx]
            parents_memories = [self.pareto['memories'][m] for m in selected_idx]
            
            # Mutates random `k` subsets of the parents
            # while ensuring the mutations fall within desired constraint limits
            mutated_population, k = [], 0
            while k < self.mutation_size:
                mutated_gene = self._mutation(random.choices(parents_population)[0])
                
                if self._check_constraints(mutated_gene) and not self._is_seen_before(mutated_gene):
                    mutated_population.append(mutated_gene)
                    k += 1

            # Crossovers random `k` subsets of the parents
            # while ensuring the crossovers fall within desired constraint limits
            crossovered_population, k = [], 0
            while k < self.crossover_size:
                crossovered_gene = self._crossover(random.sample(parents_population, 2))
                
                if self._check_constraints(crossovered_gene) and not self._is_seen_before(crossovered_gene):
                    crossovered_population.append(crossovered_gene)
                    k += 1

            # Appends current information to the logs
            logs['population'].append(copy.deepcopy(population))
            logs['params'].append(copy.deepcopy(population_params))
            logs['total_params'].append(copy.deepcopy(population_total_params))
            logs['latencies'].append(copy.deepcopy(population_latencies))
            logs['memories'].append(copy.deepcopy(population_memories))
            logs['parents'].append(copy.deepcopy(parents_population))
            logs['pareto'].append(copy.deepcopy(self.pareto))

            logs_path = os.path.join(self.results_path, f'logs_itr_{i}.pkl')
            with open(logs_path, 'wb') as f:
                pickle.dump({'population': logs['population'][-1],
                             'params': logs['params'][-1],
                             'total_params': logs['total_params'][-1],
                             'latencies': logs['latencies'][-1],
                             'memories': logs['memories'][-1],
                             'parents': logs['parents'][-1],
                             'pareto': logs['pareto'][-1]}, f)

            population = parents_population + mutated_population + crossovered_population
            assert len(population) == self.population_size
            
            self._update_population_count(population)
            self.all_population += mutated_population + crossovered_population

            self.plot_search_state(iteration=i,
                                   parents={'params': parents_params, 
                                            'total_params': parents_total_params, 
                                            'latencies': parents_latencies, 
                                            'memories': parents_memories,
                                            'population': parents_population})

        logs_path = os.path.join(self.results_path, 'logs.pkl')
        with open(logs_path, 'wb') as f:
            pickle.dump(logs, f)

        # Generates a command-line per Pareto-frontier point
        # which can be sent off to a cluster for training
        # TODO: do non-maximum suppression on the Pareto-frontier
        create_pareto_jobs(self.results_path, 
                           converter=self.converter,
                           model_type=self.model_type,
                           max_step=40000,
                           output_path=os.path.join(self.results_path, 'pareto_jobs'))    

        # Generates command-lines for fully training all architectures visited during search
        create_ground_truth_jobs(self.results_path,
                                 self.converter,
                                 model_type=self.model_type,
                                 max_step=40000,
                                 output_path=os.path.join(self.results_path, 'visited_jobs'))

    def semi_brute_force(self, n_samples: int, batch: Optional[int] = 1000) -> None:
        """Provides a brute force ablation to the evolutionary search algorithm.
        
        This method samples batches of points at random from the search space
        and updates the Pareto-frontier. Thus there is no guided sampling along the
        Pareto-frontier. 

        Args:
            n_samples: Number of genes to be sampled.
            batch: Number of batched genes to conduct the brute force.

        """

        # Samples the initial population
        path_to_population = os.path.join(self.results_path, 'init_population_bruteforce.pkl') 

        if os.path.exists(path_to_population):
            with open(path_to_population, 'rb') as f:
                population = pickle.load(f)

            population = population[:n_samples]

        else:
            population = self.sample_random_population(n_samples)

            with open(path_to_population, 'wb') as f:
                pickle.dump(population, f)

        # Samples batches of random examples from the large initial pool
        # and updates the Pareto-frontier iteratively
        for idx in range(0, n_samples, batch):
            curr_population = population[idx:idx+batch]

            curr_population_params, \
            curr_population_total_params, \
            curr_population_latencies, \
            curr_population_memories = self._calculate_constraints(curr_population)

            self.all_population += curr_population
            self.all_params += curr_population_params
            self.all_total_params += curr_population_total_params
            self.all_latencies += curr_population_latencies
            self.all_memories += curr_population_memories

            self._update_pareto_front(is_decreasing=True)

            self.plot_search_state(iter=idx)

            logs = {'population': population,
                    'params': curr_population_params,
                    'total_params': curr_population_total_params,
                    'latencies': curr_population_latencies,
                    'memories': curr_population_memories,
                    'pareto': self.pareto}

            logs_path = os.path.join(self.results_path, f'logs_bruteforce_{idx}.pkl')
            with open(logs_path, 'wb') as f:
                print(f'Saving indices: {idx}-{idx+batch}')
                pickle.dump(logs, f) 


def run_search(args: Dict[str, Any], do_brute_force: Optional[bool] = False) -> None:
    """Runs the evolutionary search.

    Args:
        args: Search-related arguments.
        do_brute_force: Employs semi brute-force to conduct the search.

    """

    alg = Evolution(**args)

    if do_brute_force:
        alg.semi_brute_force(args['n_samples'], args['batch'])
    else:
        alg.search()
