# Builtins
import datetime as dt
from logging import critical, error, info, warning, debug
from typing import Any, Dict, List, Tuple
import os.path
from pathlib import Path

# External libraries
import pandas as pd
import numpy as np
from tqdm import tqdm
import pytz

# Submodule imports
from harvest.storage import PickleStorage
import harvest.trader.trader as trader
from harvest.api.yahoo import YahooStreamer
from harvest.api.paper import PaperBroker
from harvest.storage import BaseLogger
from harvest.utils import *

class BackTester(trader.Trader):
    """
    This class replaces several key functions to allow backtesting
    on historical data. 
    """

    def __init__(self, streamer=None, config={}):      
        """Initializes the TestTrader. 
        """
        
        if streamer == None:
            self.streamer = YahooStreamer()
        else:
            self.streamer = streamer
        self.broker = PaperBroker()

        self.watchlist_global = []             # List of stocks to watch

        self.storage = PickleStorage(limit_size=False)  # local cache of historic price

        self.account = {}           # Local cash of account info 

        self.stock_positions = []   # Local cache of current stock positions
        self.option_positions = []  # Local cache of current options positions
        self.crypto_positions = []  # Local cache of current crypto positions

        self.order_queue = []       # Queue of unfilled orders 

        self.logger = BaseLogger()


    def read_pickle_data(self):
        """Function to read backtesting data from a local file. 

        :interval: The interval of the data
        :path: Path to the local data file
        :date_format: The format of the data's timestamps
        """
        for s in self.watchlist_global:
            for i in [self.interval[s]["interval"]] + self.interval[s]["aggregations"]:
                df = self.storage.open(s, i).dropna()
                if df.empty or now() - df.index[-1] > dt.timedelta(days=1):
                    warning(f"Running FETCH")
                    df = self.streamer.fetch_price_history(s, i).dropna()
                self.storage.store(s, i, df)

    def read_csv_data(self, path: str, date_format: str='%Y-%m-%d %H:%M:%S'):
        """Function to read backtesting data from a local CSV file. 

        :interval: The interval of the data
        :path: Path to the local data file
        :date_format: The format of the data's timestamps
        """
        for s in self.watchlist_global:
            for i in [self.interval[s]["interval"]] + self.interval[s]["aggregations"]:
                df = self.read_csv(f"{path}/{s}-{interval_enum_to_string(i)}.csv").dropna()
                if df.empty:
                    warning(f"Running FETCH")
                    df = self.streamer.fetch_price_history(s, i).dropna()
                self.storage.store(s, self.interval, df)
    
    def read_csv(self, path:str) -> pd.DataFrame:
        """Reads a CSV file and returns a Pandas DataFrame. 

        :path: Path to the CSV file. 
        """
        if not os.path.isfile(path):
            return pd.DataFrame()
        df = pd.read_csv(path)
        df = df.set_index(['timestamp'])
        if isinstance(df.index[0], str):
            df.index = pd.to_datetime(df.index)
        else:
            df.index = pd.to_datetime(df.index, unit='s')
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df.sort_index()
        return df
            
    def _setup(self, source, interval:str, aggregations=None, path=None):
        
        self._setup_params(interval, aggregations)
        self._setup_account()

        self.df = {}

        self.storage.limit_size = False
        if source == "PICKLE":
            self.read_pickle_data()
        elif source == "CSV":
            self.read_csv_data(path)
        else:
            raise Exception(f"Invalid source {source}. Must be 'PICKLE' or 'CSV'")
       
        conv = {
            Interval.MIN_1: 1,
            Interval.MIN_5: 5,
            Interval.MIN_15: 15,
            Interval.MIN_30: 30,
            Interval.HR_1: 60,
            Interval.DAY_1: 1440
        }

        # TODO: Skip the following step if aggregated data already exists

        # Generate the "simulated aggregation" data
        for sym in self.interval:
            interval = self.interval[sym]["interval"]
            interval_txt = interval_enum_to_string(interval)
            df = self.storage.load(sym, interval_txt)
            rows = len(df.index)
            print(f"Formatting {sym} data...")
            for agg in self.interval[sym]["aggregations"]:
                agg_txt = interval_enum_to_string(agg)
                tmp_path = f"{path}/{sym}-{interval_txt}+{agg_txt}.pickle"
                file = Path(tmp_path)
                if file.is_file():
                    continue
                print(f"Formatting aggregation from {interval_txt} to {agg_txt}...")
                points = int(conv[agg]/conv[interval])
                for i in tqdm(range(rows)):
                    df_tmp = df.iloc[0:i+1]                    
                    df_tmp = df_tmp.iloc[-points:] 
                    agg_df = aggregate_df(df_tmp, agg)
                    self.storage.store(sym, interval_txt+'+'+agg_txt, agg_df.iloc[[-1]], remove_duplicate=False)
        print("Formatting complete")

        # # Save the current state of the queue
        # for s in self.watch:
        #     self.load.append_entry(s, self.interval, self.storage.load(s, self.interval))
        #     for i in self.aggregations:
        #         self.load.append_entry(s, '-'+i, self.storage.load(s, '-'+i), False, True)
        #         self.load.append_entry(s, i, self.storage.load(s, i))

        # Move all data to a cached dataframe
        for i in [self.interval] + self.aggregations:
            i = i if i == self.interval else self.interval+'+'+i
            self.df[i] = {}
            for s in self.watch:
                df = self.storage.load(s, i, no_slice=True)
                self.df[i][s] = df.copy() 
        
        # Trim data so start and end dates match between assets and intervals
        # data_start = pytz.utc.localize(dt.datetime(1970, 1, 1))
        # data_end = pytz.utc.localize(dt.datetime.utcnow().replace(microsecond=0, second=0))
        # for i in [self.interval] + self.aggregations:
        #     for s in self.watch:
        #         start = self.df[i][s].index[0]
        #         end = self.df[i][s].index[-1]
        #         if start > data_start:
        #             data_start = start
        #         if end < data_end:
        #             data_end = end

        # for i in [self.interval] + self.aggregations:
        #     for s in self.watch:
        #         self.df[i][s] = self.df[i][s].loc[data_start:data_end]
            
        self.load_watch = True

    def start(self, interval: str='5MIN', aggregations: List[Any]=[], source: str='PICKLE', path: str="./data", kill_switch: bool=False):
        """Runs backtesting. 

        The interface is very similar to the Trader class, with some additional parameters for specifying 
        backtesting configurations.

        :param str? interval: The interval to run the algorithm on. defaults to '5MIN'.
        :param List[str]? aggregations: The aggregations to run. defaults to [].
        :param str? source: The source of backtesting data. 
            'FETCH' will pull the latest data using the broker (if specified). 
            'CSV' will read data from a locally saved CSV file.   
            'PICKLE' will read data from a locally saved pickle file, generated using the Trader class.
            defaults to 'PICKLE'.
        :param str? path: The path to the directory which backtesting data is stored. 
            This parameter must be set accordingly if 'source' is set to 'CSV' or 'PICKLE'. defaults to './data'.
        """

        for a in self.algo:
            a.config()

        self._setup(source, interval, aggregations, path)
        self.streamer.setup(self.interval, self, self.main)

        for a in self.algo:
            a.setup()
            a.trader = self
            a.watch = self.watch
            a.fetch_interval = self.fetch_interval
            
        self.main(interval)

    def main(self):
        info("Running test...")

        # import cProfile
        # pr = cProfile.Profile()
        # pr.enable()
        # Reset them 

        common_start = None
        common_end = None
        for s in self.watchlist_global:
            for i in [self.interval[s]["interval"]] + self.interval[s]["aggregations"]:
                if common_start is None or self.df[i][s].index[0] > common_start:
                    common_start = self.df[i][s].index[0]
                if common_end is None or self.df[i][s].index[-1] < common_end:
                    common_end = self.df[i][s].index[-1]
                self.storage.reset(s, i)

        self.storage.limit_size = True

        self.timestamp = common_start

        while self.timestamp < common_end:

            df_dict = {}
            for sym in self.interval:
                inter = self.interval[sym]["interval"]
                inter_txt = interval_enum_to_string(inter)
                if is_freq(self.timestamp, inter):
                    data = self.df[inter_txt][sym].loc[self.timestamp].iloc[-1] 
                    df_dict[sym] = data

            update = self._update_order_queue()
            self._update_stats(df_dict, new=update, option_update=True)
            
            for sym in self.interval:
                inter = self.interval[sym]["interval"]
                inter_txt = interval_enum_to_string(inter)
                if is_freq(self.timestamp, inter):
                    df = self.df[inter_txt][sym].loc[self.timestamp].iloc[-1] 
                    self.storage.store(s, inter_txt, df, save_pickle=False)
                    # Add data to aggregation queue
                    for agg in self.interval[sym]["aggregations"]:
                        agg_txt = interval_enum_to_string(agg)
                        df = self.df[inter_txt+'+'+agg_txt][s].loc[self.timestamp]
                        self.storage.store(s, agg, df)

            new_algo = []
            for a in self.algo:
                if not self.is_freq(self.timestamp, a.interval):
                    new_algo.append(a)
                    continue
                try:
                    a.main()
                    new_algo.append(a)
                except Exception as e:
                    self.debugging.warning(f"Algorithm {a} failed, removing from algorithm list.\nException: {e}")
            self.algo = new_algo

            self.timestamp += interval_to_timedelta(self.poll_interval)

        # pr.disable()
        # import pstats 
        # st = pstats.Stats(pr)
        # st.sort_stats('cumtime')
        # st.print_stats(0.1)
        # st.dump_stats("stat.txt")

        print(self.account)

 
    def _queue_update(self, new_df: pd.DataFrame, time):
        pass

    def _setup_account(self):
        self.account = {
            "equity": 1000000.0,
            "cash": 1000000.0,
            "buying_power": 1000000.0,
            "multiplier": 1
        }
    
    def fetch_position(self, key):
        pass

    def fetch_account(self):
        pass

  
  
