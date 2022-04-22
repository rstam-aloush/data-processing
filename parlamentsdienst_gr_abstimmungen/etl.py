import ftplib
import json
import logging
import os
import xml
import charset_normalizer
from datetime import datetime, timezone, timedelta
from xml.sax.handler import ContentHandler
from zoneinfo import ZoneInfo
import ods_publish.etl_id as odsp
import icalendar
import pandas as pd
import common
import common.change_tracking as ct
from parlamentsdienst_gr_abstimmungen import credentials


# see https://stackoverflow.com/a/33504236
class ExcelHandler(ContentHandler):
    def __init__(self):
        super().__init__()
        self.chars = []
        self.cells = []
        self.rows = []
        self.tables = []

    def characters(self, content):
        self.chars.append(content)

    def startElement(self, name, atts):
        if name == "Cell":
            self.chars = []
        elif name == "Row":
            self.cells = []
        elif name == "Table":
            self.rows = []

    def endElement(self, name):
        if name == "Cell":
            self.cells.append(''.join(self.chars))
        elif name == "Row":
            self.rows.append(self.cells)
        elif name == "Table":
            self.tables.append(self.rows)


# see https://stackoverflow.com/a/65412797
def is_file_older_than(file, delta):
    cutoff = datetime.utcnow() - delta
    mtime = datetime.utcfromtimestamp(os.path.getmtime(file))
    return True if mtime < cutoff else False


def get_session_calendar(cutoff):
    logging.info(f'Checking if we should reload the ical file from the web...')
    ical_file_path = credentials.ics_file
    cal_export_file = os.path.join(credentials.local_data_path.replace('data_orig', 'data'), 'grosser_rat_sitzungskalender.csv')
    pickle_file_name = cal_export_file.replace('.csv', '.pickle')
    if not os.path.exists(ical_file_path) or not os.path.exists(pickle_file_name) or is_file_older_than(ical_file_path, cutoff):
        logging.info(f'Pickle or iCal file does not exist or is older than required - opening Google Calendar from web...')
        url = 'https://calendar.google.com/calendar/ical/vfb9bndssqs2v9uiun9uk7hkl8%40group.calendar.google.com/public/basic.ics'
        r = common.requests_get(url=url, allow_redirects=True)
        with open(ical_file_path, 'wb') as f:
            f.write(r.content)
        logging.info(f'Parsing events into df to publish to dataset...')
        calendar = icalendar.Calendar.from_ical(r.content)
        df_cal = pd.DataFrame(dict(summary=event['SUMMARY'], dtstart=event['DTSTART'].dt, dtend=event['DTEND'].dt) for event in calendar.walk('VEVENT'))
        df_cal.to_csv(cal_export_file, index=False)
        if ct.has_changed(cal_export_file, do_update_hash_file=False):
            common.upload_ftp(cal_export_file, credentials.ftp_server, credentials.ftp_user, credentials.ftp_pass, 'parlamentsdienst/gr_sitzungskalender')
            odsp.publish_ods_dataset_by_id('100188')
            logging.info(f'Saving session calendar as pickle {pickle_file_name}...')
            df_cal.to_pickle(pickle_file_name)
            ct.update_hash_file(cal_export_file)
    else:
        logging.info(f'Reading session calendar from pickle {pickle_file_name}')
        df_cal = pd.read_pickle(pickle_file_name)
    return ical_file_path, df_cal


def is_session_now(ical_file_path, hours_before_start, hours_after_end):
    # see https://stackoverflow.com/a/26329138
    now_in_switzerland = datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Zurich'))
    # now_in_switzerland = datetime(2022, 3, 23, 10, 15, 12, 11).astimezone(ZoneInfo('Europe/Zurich'))
    with open(ical_file_path, 'rb') as f:
        calendar = icalendar.Calendar.from_ical(f.read())
    # handle case where session takes longer than defined in calendar event
    current_entries = [dict(summary=event['SUMMARY'], dtstart=event['DTSTART'].dt, dtend=event['DTEND'].dt) for event in calendar.walk('VEVENT') if event['DTSTART'].dt - pd.Timedelta(hours=hours_before_start) <= now_in_switzerland <= event['DTEND'].dt + pd.Timedelta(hours=hours_after_end)]
    session_active = True if len(current_entries) > 0 else False
    logging.info(f'Session active now? {session_active}')
    return session_active


def find_in_sheet(sheet, text_to_find):
    return [(i, text.index(text_to_find)) for i, text in enumerate(sheet) if text_to_find in text]


def handle_polls(process_archive=False, df_unique_session_dates=None):
    logging.info(f'Handling polls, value of process_archive: {process_archive}...')
    remote_path = '' if not process_archive else credentials.xml_archive_path
    xml_ls_file = credentials.ftp_ls_file.replace('.json', '_xml.json')
    xml_ls = get_ftp_ls(remote_path=remote_path, pattern='*.xml', file_name=xml_ls_file, ftp={'server': credentials.gr_polls_ftp_server, 'user': credentials.gr_polls_ftp_user, 'password': credentials.gr_polls_ftp_pass})
    df_trakt = retrieve_traktanden_pdf_filenames(process_archive, remote_path)
    if process_archive or ct.has_changed(xml_ls_file, do_update_hash_file=False):
        df_trakt = calc_traktanden_from_pdf_filenames(df_trakt)
        xml_files = common.download_ftp([], credentials.gr_polls_ftp_server, credentials.gr_polls_ftp_user, credentials.gr_polls_ftp_pass, remote_path, credentials.local_data_path, '*.xml')
        for i, file in enumerate(xml_files):
            local_file = file['local_file']
            logging.info(f'Processing file {i} of {len(xml_files)}: {local_file}...')
            if process_archive or ct.has_changed(local_file, do_update_hash_file=False):
                df_poll_details = calc_details_from_single_xml_file(local_file)
                df_merge1 = df_poll_details.merge(df_trakt, how='left', on=['session_date', 'Abst_Nr'])

                # todo: Seat 182 does not have the correct miotglied_name assigned, fix together with Sitz_Nr
                # todo: Fix other rows with Mitglied_Name like 'Vorname X Name Y'
                # Correct historical incidence of wrong seat number 182
                df_merge1.loc[df_merge1.Sitz_Nr == '182', 'Sitz_Nr'] = '60'
                # todo: Remove test polls:
                #  (a) polls outside of session days,
                #  (b) polls during session day but with a certain poll type ("Testabstimmung" or similar)
                # Remove Testabstimmungen by inner-joining session calendar with abstimmungen
                df_merge2 = df_unique_session_dates.merge(df_merge1, on=['session_date'], how='inner')
                # todo: Session cal starts 2013-05-15, polls start earlier (2021-06-05) -> Enhance session calendar to not lose old polls

                all_df = df_merge2

                # {"Datum":"2022-03-16","Zeit":"09:05:45.000","Abst_Nr":"1","Traktandum":1,"Subtraktandum":0,"Anz_J":"83","Anz_N":"1","Anz_E":"0","Anz_A":"15","Anz_P":"1","Typ":"Abstimmung","Geschaeft":"Mitteilungen und Genehmigung der Tagesordnung.","Zeitstempel_text":"2022-03-16T09:05:45.000000+0100","Sitz_Nr":"1","Mitglied_Name":"Lisa Mathys","Fraktion":"SP","Mitglied_Name_Fraktion":"Lisa Mathys (SP)","Datenstand_text":"2022-03-17T12:35:54+01:00","Entscheid_Mitglied":"J"}
                common.ods_realtime_push_df(all_df, credentials.push_url)
                export_filename_csv = local_file.replace('data_orig', 'data').replace('.xml', '.csv')
                logging.info(f'Saving data files to FTP server as backup: {local_file}, {export_filename_csv}')
                common.upload_ftp(local_file, credentials.ftp_server, credentials.ftp_user, credentials.ftp_pass, 'parlamentsdienst/gr_abstimmungsergebnisse')
                all_df.to_csv(export_filename_csv, index=False)
                common.upload_ftp(export_filename_csv, credentials.ftp_server, credentials.ftp_user, credentials.ftp_pass, 'parlamentsdienst/gr_abstimmungsergebnisse')

                ct.update_hash_file(local_file)
        ct.update_hash_file(xml_ls_file)
    return all_df


def get_unique_session_dates(df_cal):
    # df_cal['start_date'] = df_cal.dtstart.dt.strftime(date_format='%Y-%m-%d')
    # df_cal['end_date'] = df_cal.dtend.dt.strftime(date_format='%Y-%m-%d')
    # df_cal.query('start_date != end_date')
    logging.info(f'Calculating unique sesssion dates used to filter out test polls...')
    df_cal['session_date'] = df_cal.dtstart.dt.strftime(date_format='%Y%m%d')
    df_unique_cal_dates = df_cal.drop_duplicates(subset=['session_date'])[['session_date']]
    return df_unique_cal_dates


def calc_tagesordnungen_from_txt_files(process_archive=False):
    txt_ls_file = credentials.ftp_ls_file.replace('.json', '_txt.json')
    pattern = '*traktanden_col4.txt'
    txt_ls = get_ftp_ls(remote_path='', pattern=pattern, ftp={'server': credentials.gr_trakt_list_ftp_server, 'user': credentials.gr_trakt_list_ftp_user, 'password': credentials.gr_polls_ftp_pass}, file_name=txt_ls_file)
    pickle_file_name = os.path.join(credentials.local_data_path.replace('data_orig', 'data'), 'gr_tagesordnung.pickle')
    logging.info(f'Value of process_archive: {process_archive}')
    if os.path.exists(pickle_file_name) and not process_archive and not ct.has_changed(txt_ls_file, do_update_hash_file=False):
        logging.info(f'Reading tagesordnung data from pickle {pickle_file_name}...')
        df = pd.read_pickle(pickle_file_name)
    else:
        # todo: Only download changed files
        tagesordnung_files = common.download_ftp([], credentials.gr_trakt_list_ftp_server, credentials.gr_trakt_list_ftp_user, credentials.gr_trakt_list_ftp_pass, '', credentials.local_data_path, pattern)

        dfs = []
        for file in tagesordnung_files:
            logging.info(f"Cleaning file and reading into df: {file['local_file']}")

            def tidy_fn(txt: str):
                return (txt
                        .replace('\nPartnerschaftliches Geschäft', '\tPartnerschaftliches Geschäft')
                        .replace('\nJSSK', '\tJSSK')
                        .replace('\n17.5250.03', '\t17.5250.03')
                        .replace('IGPK Rhein-häfen', 'IGPK Rheinhäfen')
                        .replace('IGPK Rhein- häfen', 'IGPK Rheinhäfen')
                        .replace('IGPK Uni-versität', 'IGPK Universität')
                        .replace('IGPK Univer-sität', 'IGPK Universität')
                        .replace('Rats-büro', 'Ratsbüro')
                        .replace('00.0000.00', '\t00.0000.00')
                        .replace('12.2035.01', '\t12.2035.01')
                        .replace('Ratsbüro\t16.5326.01', 'Ratsbüro\t\t16.5326.01')
                        .replace('Ratsbüro\t16.5327.01', 'Ratsbüro\t\t16.5327.01')
                        .replace('17.0552.03', '\t17.0552.03')
                        .replace('FD\t18.5143.02', '\tFD\t18.5143.02')
                        .replace('18.5194.01', '\t18.5194.01')
                        .replace('19.5040.02', '\t19.5040.02')
                        .replace('19.5063.01', '\t19.5063.01')
                        .replace('21.0546.01', '\t21.0546.01')
                        .replace('\t18.0321.01', '18.0321.01')
                        .replace('\t18.0616.02', '18.0616.02')
                        .replace('\t17.5250.03', '17.5250.03')
                        .replace('\t18.1319.02', '18.1319.02')
                        .replace('NIM18.', 'NIM\n18.')
                        .replace('NIM17.', 'NIM\n17.')
                        .replace('NIS15.', 'NIS\n15.')
                        )
            tidy_file_name = tidy_file(file['local_file'], tidy_fn)
            df = pd.read_csv(tidy_file_name, delimiter='\t', encoding='cp1252', on_bad_lines='error', skiprows=1,
                             names=['traktand', 'title', 'commission', 'department', 'geschnr', 'info', 'col_06', 'col_07', 'col_08', 'col_09', 'col_10'],
                             dtype={'traktand': 'str', 'title': 'str', 'commission': 'str', 'department': 'str', 'geschnr': 'str', 'info': 'str', 'col_06': 'str', 'col_07': 'str', 'col_08': 'str', 'col_09': 'str', 'col_10': 'str'}
                             )
            session_date = file['remote_file'].split('_')[0]
            df['session_date'] = session_date
            df['Datum'] = session_date[:4] + '-' + session_date[4:6] + '-' + session_date[6:8]
            dfs.append(df)
            df = pd.concat(dfs)
            # remove leading and trailing characters
            df.traktand = df.traktand.str.rstrip('. ')
            df.commission = df.commission.str.lstrip(' ')
            df.department = df.department.str.strip(' ')
            df.traktand = df.traktand.fillna(method='ffill')
            df['geschaeftsnr0'] = df.geschnr.str.split('.', expand=False).str.get(0)
            df['geschaeftsnr1'] = df.geschnr.str.split('.', expand=False).str.get(1)
            df['geschaeftsnr2'] = df.geschnr.str.split('.', expand=False).str.get(2)
            df['geschaeftsnr'] = df.geschaeftsnr0 + '.' + df.geschaeftsnr1
            df['dokumentnr'] = df.geschaeftsnr0 + '.' + df.geschaeftsnr1 + '.' + df.geschaeftsnr2
            df['geschaeft-url'] = 'https://grosserrat.bs.ch/?idurl=' + df.geschaeftsnr
            df['dokument-url'] = 'https://grosserrat.bs.ch/?doknr=' + df.dokumentnr
            # Save pickle to be loaded and returned if no changes in files detected
            logging.info(f'Saving tagesordnung df to pickle {pickle_file_name}...')
            df.to_pickle(pickle_file_name)
            ct.update_hash_file(txt_ls_file)
    return df


def calc_details_from_single_xml_file(local_file):
    session_date = os.path.basename(local_file).split('_')[0]
    excel_handler = ExcelHandler()

    def tidy_fn(txt: str):
        return txt.replace('&', '+')
    xml.sax.parse(tidy_file(local_file, tidy_fn), excel_handler)
    # xml.sax.parse(tidy_xml(local_file), excel_handler)
    sheet_abstimmungen = excel_handler.tables[1]
    polls = pd.DataFrame(sheet_abstimmungen[1:], columns=sheet_abstimmungen[0])
    sheet_resultate = excel_handler.tables[0]
    # Not all sheets are the same of course, thus we have to find blocks of data using specific text
    ja_row = find_in_sheet(sheet_resultate, 'Ja')[0][0]
    details = pd.DataFrame(sheet_resultate[1:ja_row], columns=sheet_resultate[0])
    # Find 'Ja' and take the 7 rows starting there.
    sums_per_decision = pd.DataFrame(sheet_resultate[ja_row:ja_row + 6], columns=sheet_resultate[0])
    datenexport_row = find_in_sheet(sheet_resultate, 'Datenexport')[0][0]
    data_timestamp = datetime.strptime(sheet_resultate[datenexport_row + 1][1], '%Y-%m-%dT%H:%M:%S').astimezone(ZoneInfo('Europe/Zurich'))
    polls['Zeitstempel'] = pd.to_datetime(polls.Zeit, format='%Y-%m-%dT%H:%M:%S.%f').dt.tz_localize('Europe/Zurich')
    polls['Zeitstempel_text'] = polls.Zeitstempel.dt.strftime(date_format='%Y-%m-%dT%H:%M:%S.%f%z')
    polls[['Datum', 'Zeit']] = polls.Datum.str.split('T', expand=True)
    polls = polls.rename(columns={'Nr': 'Abst_Nr', 'J': 'Anz_J', 'N': 'Anz_N', 'E': 'Anz_E', 'A': 'Anz_A', 'P': 'Anz_P'})
    details['Datum'] = session_date[:4] + '-' + session_date[4:6] + '-' + session_date[6:8]
    details.columns.values[0] = 'Sitz_Nr'
    details.columns.values[1] = 'Mitglied_Name_Fraktion'
    details['Fraktion'] = details.Mitglied_Name_Fraktion.str.extract(r"\(([^)]+)\)", expand=False)
    details['Mitglied_Name'] = details.Mitglied_Name_Fraktion.str.split('(', expand=True)[[0]].squeeze().str.strip()
    details['Datenstand'] = pd.to_datetime(data_timestamp.isoformat())
    details['Datenstand_text'] = data_timestamp.isoformat()
    # todo: Get Geschaefts-ID and Document-ID from Tagesordnungen, then create links
    # See usage of Document id e.g. here: http://abstimmungen.grosserrat-basel.ch/index_archiv3_v2.php?path=archiv/Amtsjahr_2022-2023/2022.03.23
    # See document details e.g. here: https://grosserrat.bs.ch/ratsbetrieb/geschaefte/200111156
    # How to get geschaefts id from document id?
    details_long = details.melt(id_vars=['Sitz_Nr', 'Mitglied_Name', 'Fraktion', 'Mitglied_Name_Fraktion', 'Datum', 'Datenstand', 'Datenstand_text'], var_name='Abst_Nr', value_name='Entscheid_Mitglied')
    df_merge1 = polls.merge(details_long, how='left', on=['Datum', 'Abst_Nr'])
    df_merge1['session_date'] = session_date  # Only used for joining with df_trakt
    return df_merge1


def calc_traktanden_from_pdf_filenames(df_trakt):
    logging.info(f'Calculating traktandfen from pdf filenames...')
    df_trakt[['Abst', 'Abst_Nr', 'session_date', 'Zeit', 'Traktandum', 'Subtraktandum', '_Abst_Typ']] = df_trakt.remote_file.str.split('_', expand=True)
    df_trakt[['Abst_Typ', 'file_ext']] = df_trakt['_Abst_Typ'].str.split('.', expand=True)
    # Get rid of leading zeros
    df_trakt.Abst_Nr = df_trakt.Abst_Nr.astype(int).astype(str)
    df_trakt.Traktandum = df_trakt.Traktandum.astype(int)
    # Get rid of some rogue text and leading zeros
    # todo: Keep this as text in order not to fail on live imports?
    df_trakt.Subtraktandum = df_trakt.Subtraktandum.replace('Interpellationen Nr', '0', regex=False).replace('Interpellation Nr', '0', regex=False).astype(int)
    df_trakt = df_trakt[['session_date', 'Abst_Nr', 'Traktandum', 'Subtraktandum', 'Abst_Typ']]
    return df_trakt


def retrieve_traktanden_pdf_filenames(process_archive, remote_path):
    logging.info(f'Retrieving traktanden PDF filenames, value of process_archive: {process_archive}...')
    if process_archive:
        with ftplib.FTP(host=credentials.gr_polls_ftp_server) as ftp:
            ftp.login(user=credentials.gr_polls_ftp_user, passwd=credentials.gr_polls_ftp_pass)
            listing, file_list = recursive_mlsd(ftp, credentials.archive_path)
        df_trakt = pd.DataFrame(file_list, columns=['remote_file']).query("remote_file.str.contains('pdf')")
    else:
        pdf_ls_file = credentials.ftp_ls_file.replace('.json', '_pdf.json')
        pdf_ls = get_ftp_ls(remote_path=remote_path, pattern='*.pdf', file_name=pdf_ls_file, ftp={'server': credentials.gr_polls_ftp_server, 'user': credentials.gr_polls_ftp_user, 'password': credentials.gr_polls_ftp_pass})
        df_trakt = pd.DataFrame(pdf_ls)
    return df_trakt


def get_ftp_ls(remote_path, pattern, file_name, ftp):
    ls = common.download_ftp([], ftp['server'], ftp['user'], ftp['password'], remote_path, credentials.local_data_path, pattern, list_only=True)
    logging.info(f'Saving ftp ls file of pattern {pattern} to {file_name}...')
    json.dump(ls, open(file_name, 'w'), indent=1)
    return ls


# See https://codereview.stackexchange.com/q/232647
def recursive_mlsd(ftp_object, path="", maxdepth=None):
    """Run the FTP's MLSD command recursively

    The MLSD is returned as a list of tuples with (name, properties) for each
    object found on the FTP server. This function adds the non-standard
    property "children" which is then again an MLSD listing, possibly with more
    "children".

    Parameters
    ----------
    ftp_object: ftplib.FTP or ftplib.FTP_TLS
        the (authenticated) FTP client object used to make the calls to the
        server
    path: str
        path to start the recursive listing from
    maxdepth: {None, int}, optional
        maximum recursion depth, has to be >= 0 or None (i.e. no limit).

    Returns
    -------
    list
        the recursive directory listing

    See also
    --------
    ftplib.FTP.mlsd : the non-recursive version of this function
    """
    file_list = []
    if maxdepth is not None:
        maxdepth = int(maxdepth)
        if maxdepth < 0:
            raise ValueError("maxdepth is supposed to be >= 0")

    def _inner(path_, depth_):
        if maxdepth is not None and depth_ > maxdepth:
            return
        inner_mlsd = list(ftp_object.mlsd(path=path_))
        for name, properties in inner_mlsd:
            if properties["type"] == "dir":
                rec_path = path_ + "/" + name if path_ else name
                logging.info(f'Recursing into {rec_path}...')
                res = _inner(rec_path, depth_ + 1)
                if res is not None:
                    properties["children"] = res
            else:
                if name not in ['.', '..']:
                    file_list.append(name)
        return inner_mlsd, file_list

    return _inner(path, 0), file_list


def tidy_file(file_name, tidy_fn):
    """Data cleaning"""
    with open(file_name, 'rb') as f:
        raw_data = f.read()
        result = charset_normalizer.detect(raw_data)
        enc = result['encoding']
    with open(file_name, 'r', encoding=enc) as f:
        raw_txt = f.read()
    cleaned_txt = tidy_fn(raw_txt)
    filename, ext = os.path.splitext(file_name)
    clean_file = file_name.replace(ext, f'_clean{ext}')
    with open(clean_file, 'w', encoding=enc) as f:
        f.write(cleaned_txt)
    return clean_file


def handle_tagesordnungen(process_archive=False):
    df_tagesordnungen = calc_tagesordnungen_from_txt_files(process_archive)
    tagesordnungen_export_file_name = os.path.join(credentials.local_data_path.replace('data_orig', 'data'), 'grosser_rat_tagesordnungen.csv')
    df_tagesordnungen.to_csv(tagesordnungen_export_file_name, index=False)
    if process_archive or ct.has_changed(tagesordnungen_export_file_name, do_update_hash_file=False):
        common.upload_ftp(tagesordnungen_export_file_name, credentials.ftp_server, credentials.ftp_user, credentials.ftp_pass, 'parlamentsdienst/gr_tagesordnungen')
        odsp.publish_ods_dataset_by_id('100190')
        ct.update_hash_file(tagesordnungen_export_file_name)
    return df_tagesordnungen


def main():
    # todo: Set process_archive to false after go-live
    ical_file_path, df_cal = get_session_calendar(cutoff=timedelta(hours=12))
    df_unique_session_dates = get_unique_session_dates(df_cal)
    all_df = handle_polls(process_archive=True, df_unique_session_dates=df_unique_session_dates)
    df_tagesordn = handle_tagesordnungen(process_archive=True)

    if is_session_now(ical_file_path, hours_before_start=4, hours_after_end=10):
        all_df = handle_polls(process_archive=False, df_unique_session_dates=df_unique_session_dates)
    logging.info(f'Job completed successfully!')


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    logging.info(f'Executing {__file__}...')
    main()
