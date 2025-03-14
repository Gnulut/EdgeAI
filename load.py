import os,pickle
import pandas as pd
import nest_asyncio
import asyncio
import matplotlib.pyplot as plt
import time
from functools import partial
from concurrent.futures import ProcessPoolExecutor,ThreadPoolExecutor
nest_asyncio.apply() # IPython 環境中已經有 Event loop 需要做特殊處裡
from pandas.core.series import Series
from pandas.core.frame import DataFrame
from collections import Counter
import feature as feat
from typing import List, Iterator
import numpy as np

def pickle_path(subject_path:str) -> str:
    """ Get dataset path """
    BASE_PATH = os.path.join("WESAD")
    SUBJECT_PATH = os.path.join(BASE_PATH, subject_path)
    return SUBJECT_PATH

def pickle_load_sync(pickle_path:str) -> str:
    """同步函數：讀取 pickle 檔案"""
    print(f"Start pickle: {pickle_path}")
    with open(pickle_path, "rb") as f:
        data = pickle.load(f, encoding="bytes")
    print(f"End pickle: {pickle_path}")
    return data
        
class WESAD:
    def __init__(self, path_to_folder:str="WESAD", **kwargs) -> None:
        self._df = pd.DataFrame()
        self._label = None
        self._subjects = [entry.name for entry in os.scandir(path_to_folder) if entry.is_dir()]
        subject_count = kwargs.get('subject_count', len(self._subjects)) # for developing, >>REMOVE<< later
        self._subjects = self._subjects[:subject_count] # for developing, >>REMOVE<< later
        max_workers = kwargs.get('max_workers', None)
        self._executor = ProcessPoolExecutor(max_workers=max_workers)
        asyncio.run(self._build_df())  
        self.group_df = self.group(100)

    ## Data loading
    async def _load_subject_data(self, subject_str:str) -> pd.DataFrame:
        """Load data for a specific subject"""
        full_path = os.path.join(pickle_path(str(subject_str)), f"{subject_str}.pkl")
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(self._executor, pickle_load_sync, full_path)
        return self._pickle_to_df(data)
    
    def _pickle_to_df(self, data:dict) -> pd.DataFrame:
        label = data[b'label']
        subject = data[b'subject'].decode('utf-8')
        data = data[b'signal']
        data = data[b'chest']
        data = {
            'label': label,
            'subject':subject,
            **{f"ACC_{i}":x for i,x in enumerate(data[b'ACC'].T)},
            "ECG": data[b'ECG'][:,0],
            "EMG": data[b'EMG'][:,0], 
            "EDA": data[b'EDA'][:,0], 
            "Resp": data[b'Resp'][:,0], 
            "Temp": data[b'Temp'][:,0],  
        }     
        return pd.DataFrame(data)
    
    async def _build_df(self) -> None:
        """Build DataFrame by loading data from all subjects"""
        # Use asyncio.gather to load data concurrently
        tasks = [self._load_subject_data(subject) for subject in self._subjects]
        subject_dataframes = await asyncio.gather(*tasks)
        
        # Concatenate all subject dataframes
        self._df = pd.concat(subject_dataframes, ignore_index=True)
        print("Finished building DataFrame")
    
    def group(self, sample_n:int) -> pd.DataFrame:
        df = self._df[(self._df['label']==1) | (self._df['label']==2)] #「label」:1=基線（baseline），2=壓力（stress）
        df = df.groupby(['label','subject']).apply(lambda x:x.sample(n=sample_n)).reset_index(drop=True) #Sample 40 from label==1 & label==2
        self.label = df['label']
        return df
    
    ## ENDOF Data Loading
###########################################################################################################################################

    ## Feature extraction

    def rolling_window(self, feature:pd.Series, shift:int=700, window_size:int=10) -> pd.DataFrame:
        """ 
        Note: this rolling window creates 2-Dimension data, which may be too big,
              In the future this should be changed to apply function instead of creating list.
        """
        if len(feature) < window_size:
            raise IndexError(f"window size 大於 feature 的最大長度\n當前的feature長度為: {feature.size}")
        roll_obj = feature.rolling(window=window_size, step=shift)
        rows = []
        for row in roll_obj:
            if len(row) < window_size:
                continue
            rows.append(row)
        result = pd.concat(rows, ignore_index=True)
        return result

    def rolling_window_apply(self, feature:pd.Series, function_pipeline:feat.FunctionPipeline, window_size:int, shift:int=700) -> Iterator[pd.Series]:
        """ 
        Same as rolling window but ,
        intuitively the space taken will be O(n), >haven't confirmed<
        runtime will still be O(N * length * applied_func_complexity),
        need to manually implementation for functions that can be optimized to O(N * applied_func_complexity)
        """
        if len(feature) < window_size:
            raise IndexError(f"window size 大於 feature 的最大長度\n當前的feature長度為: {feature.size}")
        roll_obj = feature.rolling(window=window_size, step=shift)# too small size may cause function to blow up to O(n^2) instead of O(n)
        rows = []
        for row in roll_obj:
            if len(row) < window_size:
                continue
            result = function_pipeline.apply(row)
            rows.append(result)
        rows = pd.DataFrame(rows).T
        for row in rows.iterrows():
            yield row[1]
    
    def feature_extraction(self, sample_n:int=14000, window_size:int=7000,
                           cols:List[str]=['label', 'subject', 'ACC_0', 'ACC_1', 'ACC_2', 'ECG', 'EMG', 'EDA', 'Resp', 'Temp'], 
                           ) -> pd.DataFrame:
        # TODOS:
        # 1. change lambdas to partials
        # 2. add logic to separate different label/subject, preferably outside of this function
        # 3. (Optional) add multithreading, preferably outside of this function
        signal = self.group(sample_n=sample_n).loc[:,cols]
        features = pd.DataFrame()
        for key in cols:
            # get signal
            col_signal = signal[key]

            # get processor
            if key == 'subject': # maybe drop, 'subject' column in the future >>CHECKME<<
                continue
            if key == 'label':
                # continue # debug purposes >>DEBUG<<
                decorator = feat.FunctionPipeline([lambda x, kwargs: Counter(x).most_common(1)[0][0]], [dict()])
                col_names = [key]
            elif key == 'ECG':  
                decorator = feat.FunctionPipeline([
                    lambda x, kwargs: feat.ButterBandpass.butter_bandpass_filter(x, **kwargs),
                    lambda x, kwargs: feat.extract_hrv_frequency_features(x, **kwargs)
                ],
                [
                    dict(lowcut=10, highcut=30, fs=70),
                    dict(sampling_rate=700)
                ])
                col_names = [f"ECG_{feat}" for feat in ['ULF', 'LF', 'HF', 'UHF']]
            else:
                decorator = feat.FunctionPipeline([lambda x, kwargs: (np.std(x), np.mean(x))],[dict()])
                col_names = [f"std_{key}", f"mean_{key}"]

            # apply processor
            for col, col_name in zip(self.rolling_window_apply(col_signal, decorator, window_size=window_size), col_names):
                features[col_name] = col
        return features
    
    # def multi_T_feature_extraction(self, sample_n:int=14000, window_size:int=7000,
    #                                cols:List[str]=['label', 'subject', 'ACC_0', 'ACC_1', 'ACC_2', 'ECG', 'EMG', 'EDA', 'Resp', 'Temp'],
    #                                limit:int=0, work_n:int=1) -> pd.DataFrame:
    #     signal = self.group(sample_n=sample_n).loc[:,cols]
    #     rolling_windows = {key: self.rolling_window(signal[key],window_size=window_size) for key in cols}
        
    #     df = pd.DataFrame(rolling_windows)
    #     print(f"shape of df before filter {df.shape[0]}")
    #     df = df[df.apply(lambda row: len(set(row['label'])) == 1 and len(set(row['subject'])) == 1, axis=1)]
    #     print(f"shape of df after filter {df.shape[0]}")
    #     rolling_windows = df.to_dict(orient='list')
        
    #     max_len_of_signal = len(rolling_windows['subject'])
    #     signal_length = limit if limit > 0 else max_len_of_signal
    #     print(f"signal length: {signal_length}/{max_len_of_signal}")
        
    #     process_window_func = partial(
    #         self._process_window,
    #         rolling_windows = rolling_windows,
    #         cols = cols
    #     ) 
    #     features = []
    #     print(f"multi thread use {work_n} works")
    #     with ThreadPoolExecutor(max_workers=work_n) as exec:
    #         features = list(exec.map(process_window_func,range(signal_length)))
    #     feat_df = pd.DataFrame(features)
    #     return feat_df
    
    # def _process_window(self,i,rolling_windows,cols):
    #     col_feature = {}  # 先存這一組 window 的特徵  
    #     for key in cols:  
    #         time_signal = rolling_windows[key][i]  # 取出對應索引的窗口資料  
    #         decorator = feat.SignalDecorator(time_signal)  
    #         try:
    #             if key == 'label':
    #                 try:
    #                     col_feature['label'] = Counter(time_signal).most_common(1)[0][0]
    #                 except Exception as e:
    #                     print(time_signal)
    #             elif key == 'subject':
    #                 col_feature['subject'] = Counter(time_signal).most_common(1)[0][0]
    #             elif key == 'ECG':  
    #                 decorator.add_processor(feat.ButterBandpass(), lowcut=10, highcut=30, fs=70)  
    #                 decorator.add_processor(feat.HRVFrequency(), sampling_rate=700)  
    #                 processed_signal, results = decorator.apply()  
    #                 col_feature.update({f"ECG_{feat}": results['hrv_features'][feat] for feat in ['ULF', 'LF', 'HF', 'UHF']})   
    #                 print('.',end='')
    #             else:  
    #                 decorator.add_processor(feat.StdProcessor())  
    #                 processed_signal, results = decorator.apply()  
    #                 col_feature[f"std_{key}"] = processed_signal  
                    
    #                 decorator.add_processor(feat.MeanProcessor())  
    #                 processed_signal, results = decorator.apply()  
    #                 col_feature[f"mean_{key}"] = processed_signal      
    #         except ValueError as e:
    #             print(f"v{i}",end='')
    #     return col_feature
    
    ## ENDOF Feature Extraction

class Evaluate:
    plt.figure(figsize=(10, 6))
    @classmethod
    def with_row_limit(cls,wesad,limit_values):
        # 存儲結果的字典
        results = {
            'limit': [],
            'feature_extraction_time': [],
            'mutiT_feature_extraction_time': []
        }
        
        for limit in limit_values:
            # 測量第一個函數的執行時間
            start_time = time.time()
            wesad.feature_extraction(limit=limit)
            fe_time = time.time() - start_time
            
            # 測量第二個函數的執行時間
            start_time = time.time()
            wesad.mutiT_feature_extraction(limit=limit)
            mutiT_time = time.time() - start_time
            
            # 儲存結果
            results['limit'].append(limit)
            results['feature_extraction_time'].append(fe_time)
            results['mutiT_feature_extraction_time'].append(mutiT_time)
            print(results)
        
        
        cls.performance_data =  pd.DataFrame(results)
        plt.plot(cls.performance_data['limit'], cls.performance_data['feature_extraction_time'], 'o-', label='feature_extraction'),
        plt.plot(cls.performance_data['limit'], cls.performance_data['mutiT_feature_extraction_time'], 's-', label='mutiT_feature_extraction')
    
        return cls.performance_data
    @classmethod
    def with_diff_work(cls,wesad,work_n_s,limit=100):
        results = {'time':[],'works':[]}
        for work_n in work_n_s:
            # 測量第二個函數的執行時間
            start_time = time.time()
            wesad.mutiT_feature_extraction(limit=limit,work_n=work_n)
            mutiT_time = time.time() - start_time
            results['works'].append(work_n)
            results['time'].append(mutiT_time)
        cls.performance_data =  pd.DataFrame(results)
        plt.plot(cls.performance_data['works'], cls.performance_data['time'], 'o-', label='mutiT_feature_extraction with diff works'),
        return cls.performance_data
    @classmethod
    def show(cls):
        # 繪製性能比較圖
        if cls.performance_data.empty:
            raise ValueError("請先執行 with_XXX_XXX")
            
        plt.xlabel('limit')
        plt.ylabel('calculate cost (s)')
        plt.title('Comparison of performance')
        plt.legend()
        plt.grid(True)
        # plt.xscale('log')  # 使用對數刻度以更好地顯示不同量級的 limit
        plt.tight_layout()

        # 顯示結果表格
        print(cls.performance_data)

        # 顯示圖表
        plt.show()
        
# Use asyncio.run() in the correct context
if __name__ == '__main__':
    wesad = WESAD()
    print(wesad._df)