#!/usr/bin/env python
import csv
import re
from io import StringIO, TextIOWrapper
from itertools import starmap
import logging
import sys
import tempfile
import zipfile

import agate
import agatesql
from csvkit.convert.fixed import fixed2csv
from sqlalchemy import Column, create_engine, MetaData, Table
import sqlalchemy.types


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    '%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)


def make_schema_io(raw_field_specs):
    def make_row(row):
        start_column = int(row.group('start_column'))
        end_column = int(row.group('end_column'))
        return {
            'column': row.group('field_name'),
            'start': str(start_column),
            'length': str(end_column - start_column + 1),
            'field_type': row.group('field_type')
        }
    rows = map(make_row, raw_field_specs)

    field_names = ('column', 'start', 'length', 'field_type')
    output_io = StringIO()

    writer = csv.DictWriter(output_io, field_names)
    writer.writeheader()
    writer.writerows(rows)

    output_io.seek(0)
    return output_io


def extract_schema(readme_fragment):
    raw_field_specs = re.finditer(
        (
            r'^(?P<field_name>[A-Z][^\s]+)\s+(?:NOT NULL)?\s+' +
            r'(?P<field_type>[A-Z][^\s]+)\s+' +
            r'\((?P<start_column>\d+):(?P<end_column>\d+)\)'),
        readme_fragment, re.MULTILINE)
    schema_io = make_schema_io(raw_field_specs)
    return schema_io


def extract_table_schemas(input_zip):
    with input_zip.open('README.TXT', 'r') as readme_file:
        readme = readme_file.read().decode('utf-8')

    schemas = {}

    table_names = re.findall(r'^([A-Z][^ ]+) - ', readme, re.MULTILINE)

    def get_table_start(table_name):
        start_match = re.search(
            r'^' + table_name + ' - ', readme, re.MULTILINE)
        return (table_name, start_match.start())
    table_starts = tuple(map(get_table_start, table_names))
    last_table_name = table_names[-1]

    def get_table_end(i, table_info):
        table_name, table_start = table_info
        if table_name == last_table_name:
            table_end = None
        else:
            table_end = table_starts[i + 1][1]
        return (table_name, table_start, table_end)
    table_info = tuple(starmap(get_table_end, enumerate(table_starts)))

    for table_name, table_start, table_end in table_info:
        readme_fragment = readme[table_start:table_end]
        schema = extract_schema(readme_fragment)
        schemas[table_name] = schema

    return schemas


def get_field_type(field_type_text):
    field_components = re.match(
        r'(?P<type>[^(]+)(?:\((?P<args>.+)\))?', field_type_text)
    field_type_component = field_components.group('type')

    if field_type_component in ('VARCHAR', 'VARCHAR2'):
        length = int(field_components.group('args'))
        return sqlalchemy.types.String(length)

    if field_type_component == 'NUMBER':
        try:
            number_args = tuple(map(
                int, re.split(r',\s*', field_components.group('args'))))
        except TypeError:
            return sqlalchemy.types.BigInteger

        if len(number_args) == 1:
            length = number_args[0]
            if length < 10:
                return sqlalchemy.types.Integer
            return sqlalchemy.types.BigInteger

        if len(number_args) == 2:
            return sqlalchemy.types.Numeric(*number_args)

        raise NotImplementedError(
            'Unsure how to handle a {0}'.format(field_type_text))

    if field_type_component == 'DATE':
        return sqlalchemy.types.Date

    raise NotImplementedError(
        'Unsure how to handle a {0}'.format(field_type_text))


def ensure_table_exists(table_name, table_schema, connection):
    logger = logging.getLogger(__name__).getChild('ensure_table_exists')

    # FIXME: Why isn't this detecting the existing table? So far it isn't
    # causing any problems to attempt to recreate it, but it's still
    # concerning.
    metadata = MetaData(connection)
    if table_name in metadata.tables:
        logger.debug('Table {0} already exists'.format(table_name))
        return metadata.tables[table_name]

    table_schema.seek(0)
    schema_reader = csv.DictReader(table_schema)

    def build_column(row):
        return Column(row['column'], get_field_type(row['field_type']))

    columns = tuple(map(build_column, schema_reader))
    table = Table(table_name, metadata, *columns)
    metadata.create_all()

    logger.info('Created table {0}'.format(table_name))

    return table


def load_table(name=None, schema=None, input_zip=None, connection=None):
    logger = logging.getLogger(__name__).getChild('load_table')

    def file_is_for_table(file_name):
        if file_name == name.lower() + '.txt':
            return True
        return file_name.startswith(name.lower() + '_')
    data_file_names = tuple(filter(file_is_for_table, input_zip.namelist()))
    logger.info('Found {0} file names for table {1}: {2}'.format(
        len(data_file_names), name, ', '.join(data_file_names)))

    for data_file_name in data_file_names:
        db_table = ensure_table_exists(name, schema, connection)
        logger.debug('Ensured table {0} exists'.format(name))

        schema.seek(0)
        raw_data_file = input_zip.open(data_file_name)
        wrapped_raw_file = TextIOWrapper(raw_data_file, encoding='utf-8')

        data_csv_file = tempfile.TemporaryFile(mode='w+')
        fixed2csv(wrapped_raw_file, schema, output=data_csv_file)
        data_csv_file.seek(0)
        logger.debug('Converted raw data file {0} to temporary CSV'.format(
            data_file_name))
        wrapped_raw_file.close()
        raw_data_file.close()

        without_asterisks_file = tempfile.TemporaryFile(mode='w+')
        without_asterisks_writer = csv.writer(without_asterisks_file)
        data_csv_reader = csv.reader(data_csv_file)
        for raw_row in data_csv_reader:
            output_row = [(item if item != '*' else '') for item in raw_row]
            without_asterisks_writer.writerow(output_row)
        logger.debug('Removed asterisk-only cells')
        data_csv_file.close()

        without_asterisks_file.seek(0)
        csv_table = agate.Table.from_csv(without_asterisks_file, sniff_limit=0)
        logger.debug('Loaded CSV into agate table')
        csv_table.to_sql(
            connection, name, overwrite=False, create=False,
            create_if_not_exists=False, insert=True)
        logger.info('Done loading data file {0} into table {1}'.format(
            data_file_name, name))
        without_asterisks_file.close()


def main(input_path, database_url):
    logger = logging.getLogger(__name__).getChild('main')

    engine = create_engine(database_url)
    connection = engine.connect()
    logger.info('Connected to database at {0}'.format(database_url))

    with zipfile.ZipFile(input_path, 'r') as input_zip:
        logger.info('Opened input file {0}'.format(input_path))

        table_schemas = extract_table_schemas(input_zip)
        logger.info('Found {0} table schemas: {1}'.format(
            len(table_schemas.keys()),
            ', '.join(sorted(table_schemas.keys()))))

        table_names = sorted(table_schemas.keys())
        for table_name in table_names:
            table_schema = table_schemas[table_name]
            load_table(
                name=table_name, schema=table_schema, input_zip=input_zip,
                connection=connection)
            logger.info('Loaded table {0}'.format(table_name))

    connection.close()
    logger.info('Done')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.stderr.write(
            'Usage: {0} input_path database_url\n'.format(sys.argv[0]))
        sys.exit(1)

    main(*sys.argv[1:])
