import logging
import os
import pathlib
import zoneinfo
import zoneinfo
import pandas as pd
import xlrd
from ibs_parkhaus_bewegungen import credentials


def main():
    dfs = []
    for entry in credentials.data_files:
        logging.info(f'Parsing file {entry["file_name"]}...')
        df = pd.read_excel(entry['file_name'], skiprows=7, usecols='A:D', header=None, names=['title', 'timestamp_text', 'einfahrten', 'ausfahrten'])
        df = df.dropna(subset=['ausfahrten'])
        df.title = entry['title']
        df['timestamp'] = pd.to_datetime(df.timestamp_text, format='%Y-%m-%d %H:%M:%S').dt.tz_localize(tz='Europe/Zurich', ambiguous=True).dt.tz_convert('UTC')
        dfs.append(df)
    all_df = pd.concat(dfs)
    all_df = all_df.convert_dtypes()
    export_filename = os.path.join(pathlib.Path(__file__).parent, 'data', 'parkhaus_bewegungen.csv')
    all_df.to_csv(export_filename, index=False)
    pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    logging.info(f'Executing {__file__}...')
    main()
    logging.info(f'Job successfully completed!')
