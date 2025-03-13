import logging
from shutil import copy2
import pandas as pd
import common
from common import change_tracking as ct
from mobilitaet_verkehrszaehldaten.src import dashboard_calc
import sys
import os
import platform
import sqlite3

from dotenv import load_dotenv

load_dotenv()

PATH_ORIG = os.getenv("PATH_ORIG")
PATH_DEST = os.getenv("PATH_DEST")
FTP_SERVER = os.getenv("FTP_SERVER")
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

def parse_truncate(path, filename, dest_path, no_file_cp):
    path_to_orig_file = os.path.join(path, filename)
    path_to_copied_file = os.path.join(dest_path, filename)
    if no_file_cp is False:
        logging.info(f"Copying file {path_to_orig_file} to {path_to_copied_file}...")
        copy2(path_to_orig_file, path_to_copied_file)
    # Parse, process, truncate and write csv file
    logging.info(f"Reading file {filename}...")
    data = pd.read_csv(path_to_copied_file,
                       engine='python',
                       sep=';',
                       # encoding='ANSI',
                       encoding='cp1252',
                       dtype={'SiteCode': 'category', 'SiteName': 'category', 'DirectionName': 'category',
                              'LaneName': 'category', 'TrafficType': 'category'})
    logging.info(f"Processing {path_to_copied_file}...")
    data['DateTimeFrom'] = pd.to_datetime(data['Date'] + ' ' + data['TimeFrom'], format='%d.%m.%Y %H:%M')
    data['DateTimeTo'] = data['DateTimeFrom'] + pd.Timedelta(hours=1)
    data['Year'] = data['DateTimeFrom'].dt.year
    data['Month'] = data['DateTimeFrom'].dt.month
    data['Day'] = data['DateTimeFrom'].dt.day
    data['Weekday'] = data['DateTimeFrom'].dt.weekday
    data['HourFrom'] = data['DateTimeFrom'].dt.hour
    data['DayOfYear'] = data['DateTimeFrom'].dt.dayofyear

    # 'LSA_Count.csv'
    if 'LSA' in filename:
        logging.info(f'Creating separate files for MIV and Velo...')
        data['Zst_id'] = data['SiteCode']
        miv_data = data[data['TrafficType'] == 'MIV']
        velo_data = data[(data['TrafficType'] != 'MIV') & (data['TrafficType'].notna())]
        miv_filename = 'MIV_' + filename
        velo_filename = 'Velo_' + filename
        miv_data.to_csv(os.path.join(dest_path, miv_filename), sep=';', encoding='utf-8', index=False)
        velo_data.to_csv(os.path.join(dest_path, velo_filename), sep=';', encoding='utf-8', index=False)
        dashboard_calc.create_files_for_dashboard(data, filename, dest_path)
        generated_filenames = generate_files(miv_data, miv_filename, dest_path)
        generated_filenames += generate_files(velo_data, velo_filename, dest_path)
    # 'FLIR_KtBS_MIV6.csv', 'FLIR_KtBS_Velo.csv', 'FLIR_KtBS_FG.csv'
    elif 'FLIR' in filename:
        logging.info(f'Retrieving Zst_id as the SiteCode...')
        data['Zst_id'] = data['SiteCode']
        logging.info(f'Updating TrafficType depending on the filename for FLIR data...')
        data['TrafficType'] = 'MIV' if 'MIV6' in filename else 'Velo' if 'Velo' in filename else 'Fussgänger'
        dashboard_calc.create_files_for_dashboard(data, filename, dest_path)
        generated_filenames = generate_files(data, filename, dest_path)
    # 'MIV_Class_10_1.csv', 'Velo_Fuss_Count.csv', 'MIV_Speed.csv'
    else:
        logging.info(f'Retrieving Zst_id as the first word in SiteName...')
        data['Zst_id'] = data['SiteName'].str.split().str[0]
        logging.info(f'Creating files for dashboard for the following data: {filename}...')
        dashboard_calc.create_files_for_dashboard(data, filename, dest_path)
        generated_filenames = generate_files(data, filename, dest_path)

    logging.info(f'Created the following files to further processing: {str(generated_filenames)}')
    return generated_filenames


def generate_files(df, filename, dest_path):
    current_filename = os.path.join(dest_path, 'converted_' + filename)
    generated_filenames = []
    logging.info(f"Saving {current_filename}...")
    df.to_csv(current_filename, sep=';', encoding='utf-8', index=False)
    generated_filenames.append(current_filename)

    db_filename = os.path.join(dest_path, 'datasette', filename.replace('.csv', '.db'))
    logging.info(f'Saving into sqlite db {db_filename}...')
    conn = sqlite3.connect(db_filename)
    df.to_sql(name=db_filename.split(os.sep)[-1].replace('.db', ''), con=conn, if_exists='replace', index=False)
    common.upload_ftp(db_filename, FTP_SERVER, FTP_USER, FTP_PASS, '')

    # Only keep latest n years of data
    keep_years = 2
    current_filename = os.path.join(dest_path, 'truncated_' + filename)
    logging.info(f'Creating dataset {current_filename}...')
    latest_year = df['Year'].max()
    years = range(latest_year - keep_years, latest_year + 1)
    logging.info(f'Keeping only data for the following years in the truncated file: {list(years)}...')
    truncated_data = df[df.Year.isin(years)]
    logging.info(f"Saving {current_filename}...")
    truncated_data.to_csv(current_filename, sep=';', encoding='utf-8', index=False)
    generated_filenames.append(current_filename)

    # Create a separate dataset per year
    all_years = df.Year.unique()
    for year in all_years:
        year_data = df[df.Year.eq(year)]
        current_filename = os.path.join(dest_path, str(year) + '_' + filename)
        logging.info(f'Saving {current_filename}...')
        year_data.to_csv(current_filename, sep=';', encoding='utf-8', index=False)
        generated_filenames.append(current_filename)

    return generated_filenames


def main():
    no_file_copy = False
    if 'no_file_copy' in sys.argv:
        no_file_copy = True
        logging.info('Proceeding without copying files...')

    dashboard_calc.download_weather_station_data(PATH_DEST)

    filename_orig = ['MIV_Class_10_1.csv', 'Velo_Fuss_Count.csv', 'MIV_Speed.csv',
                     'LSA_Count.csv',
                     'FLIR_KtBS_MIV6.csv', 'FLIR_KtBS_Velo.csv', 'FLIR_KtBS_FG.csv']

    # Upload processed and truncated data
    for datafile in filename_orig:
        datafile_with_path = os.path.join(PATH_ORIG, datafile)
        if ct.has_changed(datafile_with_path):
            file_names = parse_truncate(PATH_ORIG, datafile, PATH_DEST, no_file_copy)
            if not no_file_copy:
                for file in file_names:
                    common.upload_ftp(file, FTP_SERVER, FTP_USER, FTP_PASS, '')
                    os.remove(file)
            ct.update_hash_file(datafile_with_path)

    # Upload original unprocessed data
    if not no_file_copy:
        for orig_file in filename_orig:
            path_to_file = os.path.join(PATH_DEST, orig_file)
            if ct.has_changed(path_to_file):
                common.upload_ftp(path_to_file, FTP_SERVER, FTP_USER, FTP_PASS, '')
                ct.update_hash_file(path_to_file)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    logging.info(f'Executing {__file__}...')
    logging.info(f'Python running on the following architecture:')
    logging.info(f'{platform.architecture()}')
    main()
    logging.info('Job successful!')
