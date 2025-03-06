import json
import gzip
from collections import defaultdict

# TODO: warning should show the stack as well
import warnings
import pprint
import pandas as pd
from .torch_op_mapping import op_to_perf_model_class_map
from .gpu_event_analyser import GPUEventAnalyser
from ..Trace2Tree.trace_to_tree import TraceToTree

class TreePerfAnalyzer:
    @staticmethod
    def from_file(profile_filepath, *args, **kwargs) -> "TreePerfAnalyzer":
        # Creates a TreePerfAnalyzer from the trace in the provided filepath.
        # *args, **kwargs are passed to the TreePerfAnalyzer constructor.

        if profile_filepath.endswith('.json'):
            with open(profile_filepath, 'r') as f:
                data = json.load(f)
        elif profile_filepath.endswith('.gz'):
            with gzip.open(profile_filepath, 'rt') as f:
                data = json.load(f)
        else:
            raise ValueError("Profile file should be either .json or .gz")
        
        tree = TraceToTree(data['traceEvents'])
        return TreePerfAnalyzer(tree, *args, **kwargs)

    def __init__(self, tree: TraceToTree, add_python_func=False):
        self.tree = tree
        self.add_python_func = add_python_func  
        # we check if profile contains python func events
        self.with_python_stack = next((True for event in self.tree.events if event.get('cat') == 'python_func'), False)
        self.tree.build_tree(add_python_func=add_python_func)

    def agg_kernels_in_subtree(self, event, filter_func=None, verbose=False):
        if filter_func is None:
            filter_func = lambda x: True
        if event.get('cat') in {'kernel', 'gpu_memcpy', 'gpu_memset'}:
            if not filter_func(event):
                return 0, []
            if verbose:
                print(f"Found kernel event, duration: {event['dur']}, name: {event['name']}")
            return event['dur'], [event['UID']]
        total_dur = 0
        list_kernels = []
        for child_UID in event.get('children', []):
            child = self.tree.get_UID2event(child_UID)
            child_total_dur, child_list_kernels = self.agg_kernels_in_subtree(child, filter_func, verbose)
            total_dur += child_total_dur
            list_kernels.extend(child_list_kernels)
        return total_dur, list_kernels

    def loop_and_aggregate_kernels(self, events, filter_func=None, verbose=False):
        total_kernel_time = 0
        list_kernels = []
        for event in events:
            this_total_kernel_time, this_list_kernels = self.agg_kernels_in_subtree(event, filter_func, verbose=False)
            total_kernel_time += this_total_kernel_time
            list_kernels.extend(this_list_kernels)
        return total_kernel_time, list_kernels

    @staticmethod
    def non_data_mov_filter(event):
        DATA_MOVEMENT_PATTERNS = ['at::native::direct_copy_kernel_cuda', 'transpose_']
        return not any(pattern in event['name'] for pattern in DATA_MOVEMENT_PATTERNS)

    def compute_perf_metrics(self, event, bwd=False, non_data_mov=False):

        # Handle kernel aggregation
        if bwd:
            if not event.get('bwd_events'):
                self.tree.link_bwd_events(event['UID']) 
            cpu_op_uids = event['bwd_events']
        else:
            cpu_op_uids = [event['UID']]
        cpu_op_list = [self.tree.get_UID2event(uid) for uid in cpu_op_uids]
        _, list_kernelUIDS = self.loop_and_aggregate_kernels(cpu_op_list)
        list_kernels = [self.tree.events_by_uid[uid] for uid in list_kernelUIDS]
        busy_kernel_time = 0
        if len(list_kernels) > 0:
            busy_kernel_time = GPUEventAnalyser(list_kernels).compute_metrics()['busy_time']
        _, list_non_data_mov_kernelUIDs = self.loop_and_aggregate_kernels(cpu_op_list, filter_func=self.non_data_mov_filter)
        list_non_data_mov_kernels = [self.tree.events_by_uid[uid] for uid in list_non_data_mov_kernelUIDs]
        busy_non_data_mov_time = 0
        if len(list_non_data_mov_kernels) > 0:
            busy_non_data_mov_time = GPUEventAnalyser(list_non_data_mov_kernels).compute_metrics()['busy_time']
        event['kernel_names'] = [kernel['name'] for kernel in list_kernels]

        # Select the appropriate dictionary for FLOPS and memory functions
        perf_model_class = op_to_perf_model_class_map[event['name']]
        perf_model = perf_model_class(event)

        gflops = (perf_model.flops() if not bwd else perf_model.flops_bwd())/ 1e9  

        tflops_per_s = (gflops / 1e3) / (busy_kernel_time / 1e6) if busy_kernel_time > 0 else float('nan')

        non_data_mov_tflops_per_s = (gflops / 1e3) / (busy_non_data_mov_time / 1e6) if busy_non_data_mov_time > 0 else float('nan')
        bytes_moved = perf_model.bytes() if not bwd else perf_model.bytes_bwd()

        # Return metrics
        dict_metrics = {
            'GFLOPS': gflops,
            'Kernel Time (µs)': busy_kernel_time,
            'Kernel sum Time (µs)': sum([kernel['dur'] for kernel in list_kernels]),
            'TFLOPS/s': tflops_per_s,
        }
        if non_data_mov:
            dict_metrics['Non-Data-Mov Kernel Time (µs)'] = busy_non_data_mov_time
            dict_metrics['Non-Data-Mov TFLOPS/s'] = non_data_mov_tflops_per_s
        if bytes_moved is not None:
            dict_metrics['FLOPS/Byte'] = (gflops * 1e9) / bytes_moved if bytes_moved > 0 else float('nan')
            dict_metrics['TB/s'] = (bytes_moved / 1e12) / (busy_kernel_time / 1e6) if busy_kernel_time > 0 else float('nan')
        else:
            dict_metrics['FLOPS/Byte'] = float('nan')
            dict_metrics['TB/s'] = float('nan')

        for key, value in perf_model.param_details.items():
            dict_metrics[f"param: {key}"] = value

        return dict_metrics

    def compute_fwd_perf_metrics(self, event, non_data_mov=False):
        return self.compute_perf_metrics(event, bwd=False, non_data_mov=non_data_mov)
    def compute_bwd_perf_metrics(self, event, non_data_mov=False):
        return self.compute_perf_metrics(event, bwd=True, non_data_mov=non_data_mov)
    
    def build_df_perf_metrics(self, events, bwd, non_data_mov=False, include_kernel_names=False):
        if len(events) == 0:
            warnings.warn("Input list of events is empty. Returning an empty DataFrame.")
            return pd.DataFrame()
        rows = []
        list_warn_non_zero_flops_and_zero_time = []
        list_no_bwd_events = []
        for event in events:
            metrics_event = {'cat': event['cat'], 'name': event['name'],
                             'UID': event['UID'],
                        'pid': event['pid'], 'tid': event['tid'],
                        'external_id': event['args']['External id']}
            dict_perf_metrics = self.compute_perf_metrics(event, bwd=bwd, non_data_mov=non_data_mov)

            # handle warnings
            if bwd and not event.get('bwd_events'):
                list_no_bwd_events.append(event)
                continue
            if dict_perf_metrics['GFLOPS'] > 0 and dict_perf_metrics['Kernel Time (µs)'] == 0:
                list_warn_non_zero_flops_and_zero_time.append(event)

            if dict_perf_metrics is not None:
                metrics_event.update(dict_perf_metrics)
            if include_kernel_names:
                metrics_event['kernel_names'] = event['kernel_names']
            rows.append(metrics_event)

        self._show_warnings(list_warn_non_zero_flops_and_zero_time,
                            list_no_bwd_events, len(events))
        df_perf_metrics = pd.DataFrame(rows)
        return df_perf_metrics
    
    @staticmethod
    def _show_warnings(list_warn_non_zero_flops_and_zero_time,
                          list_no_bwd_events, total_events):
        # we need to say a/b  events had this issue and one example is following
        # where b is total events
        if len(list_warn_non_zero_flops_and_zero_time) > 0:
            warnings.warn(f"Found {len(list_warn_non_zero_flops_and_zero_time)}/{total_events} events with non-zero GFLOPS and zero Kernel Time (µs).")
            warnings.warn(f"Example event: {pprint.pformat(list_warn_non_zero_flops_and_zero_time[0])}")
        if len(list_no_bwd_events) > 0:
            warnings.warn(f"Found {len(list_no_bwd_events)}/{total_events} events without backward events.")
            warnings.warn(f"Example event: {pprint.pformat(list_no_bwd_events[0])}")

                                                                                                                                        
    def build_df_fwd_perf_metrics(self, events):
        return self.build_df_perf_metrics(events, bwd=False)
    def build_df_bwd_perf_metrics(self, events):
        return self.build_df_perf_metrics(events, bwd=True)
    

    @staticmethod
    def summarize_df_perf_metrics(df_perf_metrics, agg_metrics=['mean', 'std']):
        if df_perf_metrics.empty:
            warnings.warn("Input DataFrame is empty. Returning an empty summary DataFrame.")
            return pd.DataFrame()  # Return an empty DataFrame instead of raising an error

        dict_agg = {}
        # first element for GFLOPS and FLOPS/Byte
        dict_agg['GFLOPS'] = 'first'
        if 'FLOPS/Byte' in df_perf_metrics.columns:
            dict_agg['FLOPS/Byte'] = 'first'
        if 'TB/s' in df_perf_metrics.columns:
            dict_agg['TB/s'] = agg_metrics
        dict_agg['TFLOPS/s'] = agg_metrics
        if 'Non-Data-Mov TFLOPS/s' in df_perf_metrics.columns:
            dict_agg['Non-Data-Mov TFLOPS/s'] = agg_metrics
        if 'Non-Data-Mov Kernel Time (µs)' in df_perf_metrics.columns:
            dict_agg['Non-Data-Mov Kernel Time (µs)'] = ['sum']
        dict_agg['Kernel Time (µs)'] = ['sum']
        dict_agg['name'] = 'count'  # Use the 'name' column as a proxy for counting rows

        # Identify parameter columns for grouping
        param_cols = [col for col in df_perf_metrics.columns if col.startswith('param: ')]
        #TODO warn user if nans in the performance metrics
        # Perform the aggregation
        df_perf_metrics_summary = (
            df_perf_metrics
            .groupby(['name'] + param_cols)
            .agg(dict_agg)
        )
        df_perf_metrics_summary.columns = ['_'.join(col).strip() for col in df_perf_metrics_summary.columns.values]
        df_perf_metrics_summary.reset_index(inplace=True)

        df_perf_metrics_summary.sort_values(by='Kernel Time (µs)_sum', ascending=False, inplace=True)
        df_perf_metrics_summary.reset_index(drop=True, inplace=True)

        return df_perf_metrics_summary

    def get_kernel_launchers(self, include_nccl=False):
        # This method traverses the event tree to identify CPU operations that serve as 
        # "kernel launchers." These are operations that result in GPU kernel 
        # execution without further cpu op calls. 
        # Note that kernels are called through runtime events.
        # This is why, this method identifies such cases 
        # by checking if grandchildren of CPU operations are kernel events.
        kernel_launchers = []
        for event in self.tree.events:
            if event.get('cat') != 'cpu_op':
                continue
            kernel_launcher = False
            # total_direct_kernel_time = 0
            # direct_kernel_count = 0
            list_kernels = []
            for child_UID in event.get('children', []):
                child = self.tree.events_by_uid[child_UID]
                for grand_child_UID in child.get('children', []):
                    grand_child = self.tree.events_by_uid[grand_child_UID]
                    is_kernel = grand_child.get('cat') == 'kernel'
                    is_nccl = 'nccl' in grand_child['name']
                    should_include = is_kernel and (include_nccl or not is_nccl)
                    if should_include:
                        kernel_launcher = True
                        list_kernels.append(grand_child)
            if kernel_launcher:
                event['total_direct_kernel_time'] = GPUEventAnalyser(list_kernels).compute_metrics()['busy_time']
                event['direct_kernel_count'] = len(list_kernels)
                kernel_launchers.append(event)
        return kernel_launchers

    def get_df_kernel_launchers(self, id_cols=False):

        def list_to_tuple(obj):
            if isinstance(obj, list):
                return tuple(list_to_tuple(item) for item in obj)
            return obj
        
        kernel_launchers = self.get_kernel_launchers()
        rows = []
        for event in kernel_launchers:
            metrics_event = {'name': event['name'],
                             'UID': event['UID'],
                            'total_direct_kernel_time': event['total_direct_kernel_time'],
                            'direct_kernel_count': event['direct_kernel_count']}
            for arg in ['Input Dims', 'Input type', 'Input Strides']:
                if arg in event['args']:
                    metrics_event[arg] = list_to_tuple(event['args'][arg])

            if id_cols:
                metrics_event['pid'] = event['pid']
                metrics_event['tid'] = event['tid']
                metrics_event['external_id'] = event['args']['External id']
            rows.append(metrics_event)
        df = pd.DataFrame(rows)
        return df
    
    @staticmethod
    def get_df_kernel_launchers_summary(df_kernel_launchers):
        df_temp = df_kernel_launchers.copy()
        df_agg = df_temp.groupby('name').agg({'total_direct_kernel_time': ['sum', 'count']})
        df_agg.columns = ['_'.join(col).strip() for col in df_agg.columns.values]
        df_agg.reset_index(inplace=True)
        df_agg.rename(columns={'total_direct_kernel_time_count': 'Count'}, inplace=True)
        df_agg.sort_values(by='total_direct_kernel_time_sum', ascending=False, inplace=True)
        df_agg['total_direct_kernel_time_ms'] = df_agg['total_direct_kernel_time_sum'] / 1000
        total_duration_ms = df_agg['total_direct_kernel_time_ms'].sum()
        df_agg['Percentage (%)'] = (df_agg['total_direct_kernel_time_ms'] / total_duration_ms) * 100
        df_agg['Cumulative Percentage (%)'] = df_agg['Percentage (%)'].cumsum()
        df_agg.reset_index(drop=True, inplace=True)
        
        return df_agg

    #separate out name wise perf breakdown and shape wise perf breakdown for a given name
    @staticmethod
    def get_df_kernel_launchers_summary_by_shape(df_kernel_launchers, name):
        df_temp = df_kernel_launchers.copy()
        df_temp = df_temp[df_temp['name'] == name]
        dict_agg = {'total_direct_kernel_time': ['sum', 'count', 'mean', 'std'],
                    'direct_kernel_count': ['max', 'min']}
        # df_agg = df_temp.groupby(['Input Dims']).agg(dict_agg)
        #check if the input dims and others are present in the df
        df_agg = df_temp.groupby(['Input Dims', 'Input type', 'Input Strides']).agg(dict_agg)
        df_agg.columns = ['_'.join(col).strip() for col in df_agg.columns.values]
        df_agg.reset_index(inplace=True)
        df_agg.rename(columns={'total_direct_kernel_time_sum': 'Total Kernel Time (µs)',
                               'total_direct_kernel_time_count': 'Count',
                               'total_direct_kernel_time_mean': 'Mean Kernel Time (µs)',
                               'total_direct_kernel_time_std': 'Std Kernel Time (µs)',
                               'direct_kernel_count_max': 'Max Direct Kernel Count',
                               'direct_kernel_count_min': 'Min Direct Kernel Count'}, inplace=True)
        df_agg.sort_values(by='Total Kernel Time (µs)', ascending=False, inplace=True)
        df_agg['Total Kernel Time (ms)'] = df_agg['Total Kernel Time (µs)'] / 1000
        total_duration_ms = df_agg['Total Kernel Time (ms)'].sum()
        df_agg['Percentage (%)'] = (df_agg['Total Kernel Time (ms)'] / total_duration_ms) * 100
        df_agg['Cumulative Percentage (%)'] = df_agg['Percentage (%)'].cumsum()
        df_agg.reset_index(drop=True, inplace=True)
        return df_agg

    def get_df_gpu_timeline(self):
        kernel_events =  [event for event in self.tree.events if event.get('cat') in {'kernel', 'gpu_memcpy', 'gpu_memset'}]
        gpu_event_analyser = GPUEventAnalyser(kernel_events)
        df = gpu_event_analyser.get_breakdown_df()
        return df

    def get_kernel_details(self, kernel_event):
        """
        Extract detailed information for a given kernel event.

        This method traces a kernel event's parent relationships to retrieve
        its launcher and CPU operation details, then returns a dictionary of
        relevant information. If any of the necessary links are missing or invalid,
        the function returns None.

        Args:
            kernel_event (dict): The kernel event dictionary.

        Returns:
            dict or None: A dictionary containing the kernel details, or None if linking fails.
        """
        def list_to_tuple(obj):
            # Recursively convert lists to tuples.
            return tuple(list_to_tuple(item) for item in obj) if isinstance(obj, list) else obj

        # Verify that the event is a kernel event.
        if kernel_event.get('cat') != 'kernel':
            return None

        # Attempt to trace the kernel's parent (launcher) and grandparent (CPU op) events.
        try:
            launcher = self.tree.get_UID2event(kernel_event['parent'])
            if launcher.get('cat') not in {'cuda_runtime', 'cuda_driver'}:
                return None
            cpu_op_UID = launcher['parent']
            cpu_op = self.tree.get_UID2event(cpu_op_UID)
            if cpu_op.get('cat') != 'cpu_op':
                return None
        except KeyError:
            return None

        # Build and return the details dictionary.
        return {
            'UID': kernel_event['UID'],
            'Kernel name': kernel_event['name'],
            'Kernel duration (µs)': kernel_event['dur'],
            'Kernel stream': kernel_event['args'].get('stream'),
            'Parent cpu_op UID': cpu_op_UID,
            'Parent cpu_op': cpu_op['name'],
            'Input dims': list_to_tuple(cpu_op['args'].get('Input Dims')),
            'Input types': list_to_tuple(cpu_op['args'].get('Input type')),
            'Input strides': list_to_tuple(cpu_op['args'].get('Input Strides')),
            'kernel_file': cpu_op['args'].get('kernel_file'),
            'Grid': list_to_tuple(launcher['args'].get('grid')),
            'Block': list_to_tuple(launcher['args'].get('block')),
        }


    def get_df_kernels(self):
        """
        Build a DataFrame with kernel details augmented with aggregated parent CPU op metrics.

        This method processes all events to extract kernel events, retrieves their details via
        `get_kernel_details`, and then groups kernels by their parent CPU op UID to compute:
        - The direct kernel count per CPU op.
        - The total busy time (µs) for the CPU op.
        - The percentage of the CPU op's busy time consumed by each kernel.

        For a CPU op launching only one kernel, the kernel's duration is used as the busy time and
        its percentage is set to 100%. For CPU ops launching multiple kernels, the busy time is computed
        using GPUEventAnalyser.

        Kernels that cannot be properly linked are skipped and a warning is issued.

        Returns:
            pd.DataFrame: A DataFrame containing detailed kernel information and aggregated metrics.
        """
        if self.with_python_stack:
            raise ValueError("This method does not support traces with Python stack events at the moment.")
        kernel_details_list = []
        unlinked_count = 0
        total_kernel_count = 0

        # Extract details for all kernel events.
        for event in self.tree.events:
            if event.get('cat') != 'kernel':
                continue
            total_kernel_count += 1
            details = self.get_kernel_details(event)
            if details is not None:
                kernel_details_list.append(details)
            else:
                unlinked_count += 1

        # Group kernel events by their parent CPU op UID.
        cpu_op_to_kernel_events = defaultdict(list)
        for details in kernel_details_list:
            parent_uid = details['Parent cpu_op UID']
            cpu_op_to_kernel_events[parent_uid].append(self.tree.get_UID2event(details['UID']))

        # Compute busy time for each CPU op.
        cpu_op_to_busy_time = {}
        for parent_uid, events in cpu_op_to_kernel_events.items():
            if len(events) == 1:
                # For a single kernel, use its own duration.
                busy_time = events[0]['dur']
            else:
                busy_time = GPUEventAnalyser(events).compute_metrics()['busy_time']
            cpu_op_to_busy_time[parent_uid] = busy_time

        # Augment each kernel's details with aggregated metrics.
        for details in kernel_details_list:
            parent_uid = details['Parent cpu_op UID']
            kernel_count = len(cpu_op_to_kernel_events[parent_uid])
            busy_time = cpu_op_to_busy_time[parent_uid]
            details['Parent cpu_op direct kernel count'] = kernel_count
            details['Parent cpu_op busy time (µs)'] = busy_time
            if kernel_count == 1:
                details['Percent of Parent cpu_op busy time (%)'] = 100.0
            else:
                details['Percent of Parent cpu_op busy time (%)'] = (
                    (details['Kernel duration (µs)'] / busy_time * 100) if busy_time > 0 else float('nan')
                )

        if unlinked_count > 0:
            warnings.warn(
                f"Found {unlinked_count}/{total_kernel_count} kernels without host link. They are not included in the DataFrame."
            )

        df_kernel_view = pd.DataFrame(kernel_details_list)
        df_kernel_view.reset_index(drop=True, inplace=True)
        return df_kernel_view

