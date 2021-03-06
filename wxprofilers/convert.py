# -*- coding: utf-8 -*-
"""Functions for importing data"""
import io, re, datetime, warnings
import xml.etree.ElementTree
import numpy as np
import pandas as pd
import xarray as xr

class MultipleScansException(Exception):
    def __init__(self, value):
        self.parameter = value
    def __str__(self):
        return repr(self.parameter)

class NoScansException(Exception):
    def __init__(self, value):
        self.parameter = value
    def __str__(self):
        return repr(self.parameter)

class ScanNotFoundException(Exception):
    def __init__(self, value):
        self.parameter = value
    def __str__(self):
        return repr(self.parameter)

def lidar_from_csv(rws, sequences=None, scans=None, scan_id=None, wind=None, attrs=None):
    """create a lidar object from Nathan's csv files"""

    # start with the scan info
    if scans is not None:
        scan_file_xml = xml.etree.ElementTree.parse(scans).getroot()
        if len(scan_file_xml) == 0:
            raise NoScansException('no scans listed in the scan.xml file')
        elif scan_id is not None:
            # get the scan ID's
            scan_ids = list(map(lambda x: int(x.get('id')), scan_file_xml.findall('lidar_scan')))
            # get the corresponding scan info
            try:
                scan_index = scan_ids.index(scan_id)
            except ValueError:
                raise ScanNotFoundException('scan not found in scan.xml file')
            scan_xml = scan_file_xml[scan_index]
        else:
            if len(scan_file_xml) > 1:
                raise MultipleScansException('must provide a scan_id if file contains multiple scanning modes')
            scan_xml = scan_file_xml[0]
        scan_info = scan_xml[1][2][0].attrib
        # add prefix 'scan' to all scan keys
        scan_info = { 'scan_' + key: value for (key, value) in scan_info.items() }
        # add scan info to the lidar attributes
        if attrs is None:
            attrs = scan_info
        else:
            attrs.update(scan_info)
    else:
        scan = None

    # do this differently depending on the software version
    try: 
        dtypes = {'Timestamp': str, 'Configuration ID': int,
                  'Scan ID': int, 'LOS ID': int, 'Azimuth [°]': float,
                  'Elevation [°]': float, 'Range [m]': float, 'RWS [m/s]': float,
                  'DRWS [m/s]': float, 'CNR [db]': float, 'Confidence Index [%]': float,
                  'Mean Error': float, 'Status': bool}
        csv = pd.read_csv(rws, parse_dates=['Timestamp'], dtype=dtypes)
        name_dict = {'Timestamp': 'Time', 'RWS [m/s]': 'RWS', 'DRWS [m/s]': 'DRWS', 'CNR [db]': 'CNR',
                     'Range [m]': 'Range', 'Configuration ID': 'Configuration', 'LOS ID': 'LOS',
                     'Azimuth [°]': 'Azimuth', 'Elevation [°]': 'Elevation'}
        if 'Confidence Index [%]' in csv.columns:
            name_dict['Confidence Index [%]'] = 'Confidence'
        if 'Mean Error' in csv.columns:
            name_dict['Mean Error'] = 'Error'
        csv.rename(columns=name_dict, inplace=True)
        profile_vars = ['LOS', 'Configuration', 'Azimuth', 'Elevation']
    except ValueError:
        # this happens if we're looking at a newer version of the data
        # file using semicolons as separators
        dtypes = {'Timestamp': str, 'Settings ID': int, 'Resolution ID': int,
                  'Scan ID': int, 'LOS ID': int, 'Sequence ID': int, 'Azimuth [°]': float,
                  'Elevation [°]': float, 'Range [m]': float, 'Radial Wind Speed [m/s]': float,
                  'Dispersion Radial Wind Speed [m/s]': float, 'CNR [dB]': float,
                  'Confidence Index [%]': float, 'Mean Error': float, 'Status': bool}
        csv = pd.read_csv(rws, sep=';', parse_dates=['Timestamp'], dtype=dtypes)
        name_dict = {'Timestamp': 'Time', 'Radial Wind Speed [m/s]': 'RWS',
                     'Dispersion Radial Wind Speed [m/s]': 'DRWS', 'CNR [dB]': 'CNR',
                     'Range [m]': 'Range', 'Configuration ID': 'Configuration', 'LOS ID': 'LOS',
                     'Azimuth [°]': 'Azimuth', 'Elevation [°]': 'Elevation',
                     'Settings ID': 'Settings', 'Resolution ID': 'Resolution',
                     'Confidence Index [%]': 'Confidence', 'Mean Error': 'Error',
                     'Sequence ID': 'Sequence'}
        csv.rename(columns=name_dict, inplace=True)
        profile_vars = ['LOS', 'Settings', 'Resolution', 'Azimuth', 'Elevation', 'Sequence']
        
    if scan_id is not None:
        csv = csv.loc[csv['Scan ID'] == scan_id]
        # check that there's still data here:
        # ...

    # organize the data
    data = csv.drop(profile_vars, 1).pivot(index='Time', columns='Range')
    data.index = pd.to_datetime(data.index)

    # these fields will be variables in the xarray object
    # remove columns that don't exist in the csv file (for example, if not using the whole radial wind data)
    measurement_vars = ['RWS', 'DRWS', 'CNR', 'Confidence', 'Error', 'Status']
    measurement_vars = list(set(measurement_vars) & set(csv.columns))

    # add sequences if we got a sequences.csv file
    if sequences is not None:
        seq_csv = pd.read_csv(sequences, parse_dates=[3, 4])
        # the 'Last Acquisition' time is rounded down to the nearest
        # second, so add a second so that the time range includes the
        # last scan
        seq_csv['Last Acquisition'] += pd.Timedelta('1 second')
        csv['Sequence'] = None
        profile_vars.append('Sequence')
        # find the matching sequences using first and last times
        seq_indices = np.searchsorted(seq_csv['Last Acquisition'],
                                      csv['Time'])
        seq_indices2 = np.searchsorted(seq_csv['First Acquisition'],
                                       csv['Time']) - 1
        # make sure all the scans fell into a sequence time range
        seq_matches = seq_indices == seq_indices2
        csv['Sequence'] = seq_csv['Sequence'][seq_indices].values
        if not seq_matches.all():
            warnings.warn('Some profiles have no matching sequence')
            csv.loc[~seq_matches, 'Sequence'] = None
        

    # get profile-specific variables
    profile_vars.append('Time')
    csv_profs = csv[profile_vars].drop_duplicates()
    # should check here that each time has unique values
    # ...

    h1 = {}
    coords = {'Time': ('Time', data.index), 'Range': data.columns.levels[1]}
    if wind is not None:
        coords['Component'] = ('Component', ['x', 'y', 'z'])
    # get rid of 'Timestamp' since it's already in the coords variable
    profile_vars.remove('Time')

    for scan_type in profile_vars:
        coords[scan_type] = ('Time', csv_profs[scan_type])
    for level in measurement_vars:
        h1[level] = (('Time', 'Range'), xr.DataArray(data[level]))

    ds = xr.Dataset(h1, coords=coords, attrs=attrs)
    # name_dict = {'Timestamp': 'Time', 'RWS [m/s]': 'RWS', 'DRWS [m/s]': 'DRWS', 'CNR [db]': 'CNR',
    #              'Range [m]': 'Range', 'Configuration ID': 'Configuration', 'LOS ID': 'LOS',
    #              'Azimuth [°]': 'Azimuth', 'Elevation [°]': 'Elevation',
    #              'Settings ID': 'Settings', 'Resolution ID': 'Resolution'}
    # if sequences is not None:
    #     name_dict['Sequence ID'] = 'Sequence'
    # ds.rename(name_dict, inplace=True)

    # set the units
    ds['RWS'].attrs['long_name'] = 'radial wind speed'
    ds['RWS'].attrs['units'] = 'm/s'
    ds['DRWS'].attrs['long_name'] = 'deviation of radial wind speed'
    ds['DRWS'].attrs['units'] = 'm/s'
    ds['CNR'].attrs['long_name'] = 'carrier to noise ratio'
    ds['CNR'].attrs['units'] = 'dB'
    ds.coords['Azimuth'].attrs['standard_name'] = 'sensor_azimuth_angle'
    ds.coords['Azimuth'].attrs['units'] = 'degree'
    ds.coords['Elevation'].attrs['long_name'] = 'elevation'
    ds.coords['Elevation'].attrs['units'] = 'degree'

    if 'Confidence' in measurement_vars:
        ds['Confidence'].attrs['standard_name'] = 'confidence index'
        ds['Confidence'].attrs['units'] = 'percent'

    if 'Status' in measurement_vars:
        ds['Status'] = ds['Status'].astype(bool)
        ds['Status'].attrs['long_name'] = 'status'

    if 'Error' in measurement_vars:
        ds['Error'].attrs['long_name'] = 'mean error'

    if not wind is None:
        wind_csv = pd.read_csv(wind)
        wind_csv['TimeStamp'] = pd.to_datetime(wind_csv['TimeStamp'])

        wind_extra = ['Azimuth [°]', 'Elevation [°]', 'CNR [db]', 'Confidence index [%]']
        wind_small = wind_csv.drop(wind_extra, 1).pivot(index='TimeStamp', columns='Range [m]')
        #return wind_small

        # remove reconstructed wind data that's outside the range of
        # the rws data (this is such a terrible data storage system,
        # yuck)
        rws_max = ds.coords['Time'].values.max()
        wind_small = wind_small[wind_small.index <= rws_max]

        # # this would be totally stupid
        # wind_long = wind_csv.drop(wind_extra, 1).pivot(index=)

        # use this to find the corresponding timestamps (it works I swear!)
        # return ds
        row_indices = np.searchsorted(ds.coords['Time'].values,
                                      wind_small.index.values)
        col_indices = np.searchsorted(ds.coords['Range'].values,
                                      wind_small.columns.levels[1].values)
        
        wspeed_dims = ('Component', 'Time', 'Range')
        ds['Windspeed'] = xr.DataArray(np.full(tuple( ds.dims[dim] for dim in wspeed_dims ), np.nan, float),
                                           dims=wspeed_dims)
        # return ds, wind_small
        ds['Windspeed'][0, row_indices, col_indices] = -wind_small['Y-Wind Speed [m/s]']
        ds['Windspeed'][1, row_indices, col_indices] = -wind_small['X-Wind Speed [m/s]']
        ds['Windspeed'][2, row_indices, col_indices] = -wind_small['Z-Wind Speed [m/s]']
        # doesn't work, nice try though:
        # ds['Windspeed'][:, row_indices, col_indices] = -wind_small.loc[:, ['Y-Wind Speed [m/s]', 'X-Wind Speed [m/s]', 'Z-Wind Speed [m/s]']]
        ds['Windspeed'].attrs['long_name'] = 'wind speed'
        ds['Windspeed'].attrs['units'] = 'm/s'

    ds.coords['Range'].attrs['standard_name'] = 'height'
    ds.coords['Range'].attrs['units'] = 'm'
    ds.coords['Time'].attrs['standard_name'] = 'time'
    
    return ds


def mwr_from_csv(file, scan='Zenith', resample=None, attrs=None, resample_args={'keep_attrs': True}):
    # read file
    f = open(file, "r")
    lines = f.readlines()
    f.close()

    # get the type of each line
    types = [int(re.sub(",.*", "", re.sub("^[^,]*,[^,]*,", "", line))) for line in lines]
    headers = np.where([re.search("^Record", line) for line in lines])

    # organize into csv's
    csvs = {}
    for n in np.nditer(headers):
        # accept the type number plus 1-4
        acceptable_types = np.array([1, 2, 3, 4])
        acceptable_types += types[n]
        is_type = [types[m] in acceptable_types for m in range(len(types))]
        where_is_type = np.where(is_type)
        if where_is_type[0].size > 0:
            # get the data lines of the csv
            csv_lines = [lines[m] for m in np.nditer(where_is_type)]
            # insert the corresponding csv header line
            csv_lines.insert(0, lines[n])
            csv_string = ''.join(csv_lines)
            # this is the python 2 version-- not supported!
            # csv = io.StringIO(csv_string.decode('utf-8'))
            csv = io.StringIO(csv_string)
            df = pd.read_csv(csv, skipinitialspace=True, na_values='*******')
            csvs[str(types[n])] = df

    record_types = csvs['100']['Title'].values
    # get the names and units out of the 'record type' strings
    names = [ re.split(' \(', record_type)[0] for record_type in record_types ]
    units = [ re.sub('.*\(|\).*', '', record_type) for record_type in record_types ]
    record_unit_dict = {}
    for n in range(len(record_types)):
        record_unit_dict[names[n]] = units[n]

    mr_data = {}

    csvs['400']['DataQuality'] = csvs['400']['DataQuality'].astype(bool)
    df400 = csvs['400']
    df400['Date/Time'] = pd.to_datetime(df400['Date/Time'])
    for n in range(csvs['100'].shape[0]):
        name = names[n]
        is_type = ((df400['400'] == csvs['100']['Record Type'][n]) &
                   (df400['LV2 Processor'] == scan))
        df = df400.loc[is_type, df400.columns[4:-1]]
        df.index = df400.loc[is_type, 'Date/Time']
        df.columns = df.columns.map(float)
        mr_data[name] = df

    # convert data frame to xarray
    mrdf = df400
    # add a scan number (like record number, but for all measurements together)
    mrdf['scan'] = np.floor_divide(range(mrdf.shape[0]), 16)
    mrdf.set_index(['scan', '400', 'LV2 Processor'], inplace=True)
    mrdf2 = mrdf.drop(['Record', 'DataQuality', 'Date/Time'], axis=1)
    # mrdf2 = mrdf.drop(['DataQuality', 'Date/Time'], axis=1)
    # temp = mrdf2.loc[mrdf2['']]
    mrxr = xr.DataArray(mrdf2).unstack('dim_0')
    mrtimes = xr.DataArray(mrdf.loc[(mrdf.index.get_level_values('400') == 401) &
                                    (mrdf.index.get_level_values('LV2 Processor') == 'Zenith')]['Date/Time'])
    # mrtimes = mrtimes.values
    # print(mrtimes)
    # mrtimes.reset_index(drop=True, inplace=True)
    # mrtimes = xr.DataArray(mrdf['Date/Time']).unstack('dim_0')
    # mrds = xr.Dataset({'Measurement': mrxr, 'Date/Time': mrtimes}, attrs=attrs)
    mrds = xr.Dataset({'Measurement': mrxr}, attrs=attrs)
    mrds['DataQuality'] = xr.DataArray(mrdf['DataQuality']).unstack('dim_0')
    mrds.coords['dim_1'] = mrxr.coords['dim_1'].values.astype(float)
    mrds.rename({'400': 'Record Type', 'dim_1': 'Range'}, inplace=True)
    # create a record type dictionary and replace 401 etc with Temperature etc
    rt_dict = { csvs['100']['Record Type'][n]: names[n] for n in range(csvs['100'].shape[0]) }
    mrds.coords['Record Type'] = [ rt_dict[n] for n in mrds.coords['Record Type'].values ]
    # mrds.coords['Record Type'] = names
    mrds.coords['Range'].attrs['units'] = 'km'
    mrds['Measurement'].attrs['units'] = record_unit_dict
    mrds['Time'] = ('scan', mrtimes.values)
    mrds.set_coords('Time', inplace=True)
    mrds.swap_dims({'scan': 'Time'}, inplace=True)
    mrds = mrds.drop('scan')

    # mrds.rename({'Date/Time': 'Time'}, inplace=True)

    if resample is None:
        return mrds
    else:
        # mwrds2 = mrds.rasp.nd_resample('5T', 'Time', 'scan').rasp.split_array('Measurement', 'Record Type')
        mwrds2 = mrds.rasp.nd_resample('5T', 'Time', 'scan').rasp.split_array('Measurement', 'Record Type')
        mwrds2['Temperature'].attrs['units'] = 'K'
        mwrds2['Vapor Density'].attrs['units'] = '?'
        mwrds2['Relative Humidity'].attrs['units'] = '%'
        mwrds2['Liquid'].attrs['units'] = 'g/m^3'
        if not attrs is None:
            mwrds2.attrs = attrs
        return mwrds2

def weather_balloon(fname):
    # get metadata
    fo = open(fname, 'r')
    lines = fo.readlines()
    fo.close()
    # header_end = np.where(np.equal(lines, '\r\n'))
    header_end = np.where([line == '\r\n' for line in lines])[0][0]
    lines = lines[0:header_end]
    lines = [line.strip() for line in lines]
    metadata = {}
    for line in lines:
        parts = line.split(' : ')
        key = parts[0].strip()
        value = parts[1]
        metadata[key] = value
        # get the date
    bdate = pd.to_datetime(metadata['Flight'].split(', ')[1])
    btime = pd.to_datetime(metadata['Flight'].split(', ')[2])
    bstart = btime.replace(year=bdate.year, month=bdate.month, day=bdate.day)

    # read data
    b1 = pd.read_csv('../data/balloon/FLT_010117_0000_PROC.txt.TXT', sep=';', index_col=False, skiprows=header_end + 1)

    # add the starting date to the timestamp field
    b1['Time Stamp'] = str(bdate.date()) + ' ' + b1['Time Stamp']
    b1['Time Stamp'] = pd.to_datetime(b1['Time Stamp'], format='%Y-%m-%d %H:%M:%S')

    # cumulative sum dates to correct the date changes
    time_lower = (b1['Time Stamp'].values[1:] - b1['Time Stamp'].values[0:-1]).astype(float) < 0
    elapsed_time = (b1['Elapsed Time'].values[1:] - b1['Elapsed Time'].values[0:-1]) >= 0
    days_ahead = np.cumsum(np.logical_and(time_lower, elapsed_time))
    days_ahead = np.insert(days_ahead, 0, 0)
    days_ahead = pd.to_timedelta(days_ahead, unit='D')
    b1['Time Stamp'] += days_ahead

    b1.set_index('Time Stamp', inplace=True)

    # xrb = xr.Dataset(b1, attrs=metadata)
    xrb = xr.Dataset(b1)
    xrb = xrb.expand_dims('Station').expand_dims('Profile')
    xrb.set_coords(
        ['Elapsed Time', 'Geopotential Height', 'Corrected Elevation', 'Latitude', 'Longitude', 'Geometric Height'],
        inplace=True)

    # set up the station coordinates
    xrb.coords['Station'] = ('Station', [metadata['Station Name (WMO #)']])
    xrb.coords['Station Height'] = ('Station', [metadata['Station Height']])
    xrb.coords['Station Latitude'] = ('Station', [metadata['Station Latitude']])
    xrb.coords['Station Longitude'] = ('Station', [metadata['Station Longitude']])

    # set up the profile coordinates
    xrb.coords['Profile'] = ('Profile', [bstart])
    xrb.coords['Flight'] = ('Profile', [metadata['Flight']])
    xrb.coords['File Name'] = ('Profile', [metadata['File Name']])
    xrb.coords['Observer Initial'] = ('Profile', [metadata['Observer Initial']])
    xrb.coords['Version #'] = ('Profile', [metadata['Version #']])
    return xrb
