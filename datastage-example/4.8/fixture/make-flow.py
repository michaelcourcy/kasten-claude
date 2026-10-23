#!/usr/bin/env python3
"""Generate customers-etl.json — the DataStage flow used as this blueprint's example data.

The flow is four stages:

    Row_Generator_1 --> Copy_1 --+--> customers_txt   Sequential File -> /px-storage/data/customers.txt
       (5 records)               +--> customers_ds    Data Set        -> customers.ds

Two targets on purpose: a Sequential File writes an ordinary file onto the engine's px-storage
volume, while a Data Set writes the parallel engine's own format, whose descriptor lands on
ds-storage and whose data lands in /px-storage/pds_files/node*. One run therefore touches every
place DataStage persists something. The Row Generator source keeps the flow free of any external
dependency.

The format is the common-pipeline "pipeline-flow-v3" schema with DataStage specifics under
app_data.datastage. Regenerate with:

    python3 fixture/make-flow.py > fixture/customers-etl.json

Identifiers are fixed rather than random so that regenerating produces a byte-identical file.

Two things the parallel engine rejects, learned the hard way:
  - a column may not be called "name" — the job aborts at initializeFromArgs() with
    'schema contains a field "name" that is a subrec or tagged'
  - every stage needs its inputcolProperties to agree with the schema it is given
"""

import json
import sys

# Column set. Kept all-CHAR: the Row Generator fills them with default patterns, which is enough
# to prove rows flowed, and avoids the type-mapping subtleties of numeric columns.
COLS = [("cust_name", 20), ("country", 2), ("amount", 6)]

# Fixed identifiers, so regeneration is deterministic.
N_GEN = "10000000-0000-4000-8000-000000000001"
N_CPY = "10000000-0000-4000-8000-000000000002"
N_TXT = "10000000-0000-4000-8000-000000000003"
N_DS = "10000000-0000-4000-8000-000000000004"
P_GEN = "20000000-0000-4000-8000-000000000001"
P_CPY1 = "20000000-0000-4000-8000-000000000002"
P_CPY2 = "20000000-0000-4000-8000-000000000003"
I_CPY = "20000000-0000-4000-8000-000000000004"
I_TXT = "20000000-0000-4000-8000-000000000005"
I_DS = "20000000-0000-4000-8000-000000000006"
L1 = "30000000-0000-4000-8000-000000000001"
L2 = "30000000-0000-4000-8000-000000000002"
L3 = "30000000-0000-4000-8000-000000000003"
S_GEN = "40000000-0000-4000-8000-000000000001"
S_TXT = "40000000-0000-4000-8000-000000000002"
S_DS = "40000000-0000-4000-8000-000000000003"
PIPE = "50000000-0000-4000-8000-000000000001"
FLOW = "50000000-0000-4000-8000-000000000002"

TARGET_FILE = "/px-storage/data/customers.txt"
DATASET_NAME = "customers.ds"


def field(name, length, source_link=None):
    metadata = {
        # item_index is the column's NESTING LEVEL, not its position. Numbering the columns
        # 0,1,2 makes the compiler emit "cust_name:subrec ( country:subrec ( amount:... ) )"
        # and the job then aborts with 'schema contains a field ... that is a subrec or
        # tagged; only top-level fields are accepted'. Flat columns are all level 0.
        "item_index": 0,
        "is_key": False,
        "min_length": length,
        "max_length": length,
        "decimal_scale": 0,
        "decimal_precision": 0,
        "description": "",
        "is_signed": True,
    }
    if source_link:
        metadata["source_field_id"] = f"{source_link}.{name}"
    return {
        "metadata": metadata,
        "nullable": False,
        "name": name,
        "app_data": {
            "time_scale": 0,
            "odbc_type": "CHAR",
            "is_unicode_string": False,
            "type_code": "STRING",
        },
        "type": "string",
    }


def schema(schema_id, source_link=None):
    return {
        "id": schema_id,
        "fields": [field(n, l, source_link) for n, l in COLS],
    }


def column_properties():
    """The stage-level view of the same columns. Must agree with the schema."""
    return [
        {
            "Unicode": False,
            "Description": "",
            "Signed": True,
            "Metadata": f"CHAR({length})",
            "Scale": 0,
            "TimeScale": 0,
            "OldColumnName": name,
            "TempOldColumnName": name,
            "ColumnName": name,
            "Length": length,
            "MisFieldProperties": "",
            "DataType": "CHAR",
            "Key": False,
            "Nullable": False,
        }
        for name, length in COLS
    ]


def link(source_node, source_port, link_id, link_name):
    return {
        "node_id_ref": source_node,
        "type_attr": "PRIMARY",
        "id": link_id,
        "link_name": link_name,
        "app_data": {
            "datastage": {},
            "ui_data": {
                "decorations": [
                    {
                        "temporary": False,
                        "label_allow_return_key": "save",
                        "label_single_line": True,
                        "label_editable": True,
                        "width": 100,
                        "height": 80,
                        "x_pos": -18,
                        "y_pos": -20,
                        "id": "dec-8",
                        "position": "middle",
                        "label": link_name,
                    }
                ]
            },
        },
        "port_id_ref": source_port,
    }


row_generator = {
    "op": "PxRowGenerator",
    "id": N_GEN,
    "type": "binding",
    "outputs": [
        {
            "id": P_GEN,
            "schema_ref": S_GEN,
            "app_data": {
                "datastage": {"is_source_of_link": L1},
                "ui_data": {"label": "outPort", "cardinality": {"min": 1, "max": 1}},
                "additionalProperties": {"enableAcp": True},
            },
            "parameters": {
                "records": 5,
                "buf_mode": "default",
                "enableSchemalessDesign": False,
            },
        }
    ],
    "app_data": {
        "ui_data": {
            "image": "/data-intg/flows/graphics/palette/PxRowGenerator.svg",
            "x_pos": 64.0,
            "y_pos": 174.0,
            "label": "Row_Generator_1",
        }
    },
    "parameters": {
        "combinability": "auto",
        "output_count": 1,
        "input_count": 0,
        "execmode": "default_seq",
        "preserve": -3,
    },
}

copy_stage = {
    "op": "PxCopy",
    "id": N_CPY,
    "type": "execution_node",
    "inputs": [
        {
            "id": I_CPY,
            "schema_ref": S_GEN,
            "links": [link(N_GEN, P_GEN, L1, "Link_1")],
            "app_data": {"ui_data": {"label": "inPort", "cardinality": {"min": 1, "max": 1}}},
            "parameters": {"runtime_column_propagation": 0},
        }
    ],
    "outputs": [
        {
            "id": port,
            "schema_ref": schema_ref,
            "app_data": {
                "datastage": {"is_source_of_link": link_id},
                "ui_data": {"label": "outPort", "cardinality": {"min": 0, "max": 2147483647}},
                "additionalProperties": {"enableAcp": True},
            },
            "parameters": {"buf_mode": "default"},
        }
        for port, schema_ref, link_id in ((P_CPY1, S_TXT, L2), (P_CPY2, S_DS, L3))
    ],
    "app_data": {
        "ui_data": {
            "image": "/data-intg/flows/graphics/palette/PxCopy.svg",
            "x_pos": 200.0,
            "y_pos": 174.0,
            "label": "Copy_1",
        }
    },
    "parameters": {
        "combinability": "auto",
        "showPartType": True,
        "showCollType": False,
        "showSortOptions": False,
        "inputcolProperties": column_properties(),
        "output_count": 2,
        "input_count": 1,
        "execmode": "default_par",
        "force": " ",
        "enableSchemalessDesign": False,
        "preserve": -3,
        "inputName": "Link_1",
    },
}

sequential_file = {
    "op": "PxSequentialFile",
    "id": N_TXT,
    "type": "binding",
    "outputs": [
        {"id": "", "app_data": {"ui_data": {"label": "outPort", "cardinality": {"min": 0, "max": 1}}}}
    ],
    "inputs": [
        {
            "id": I_TXT,
            "schema_ref": S_TXT,
            "links": [link(N_CPY, P_CPY1, L2, "Link_2")],
            "app_data": {"ui_data": {"label": "inPort", "cardinality": {"min": 0, "max": 1}}},
            "parameters": {
                "file": [TARGET_FILE],
                "file_location": "file_system",
                "file_format": "sequential",
                "append-overwrite": "overwrite",
                "delim": "','",
                "quote": "double",
                "record_delim": "'\\n'",
                "final_delim": "none",
                "firstLineColumnNames": " ",
                "null_field": '""',
                "null_field_sep_flag": False,
                "rejects": "continue",
                "writemethod": " ",
                "nocleanup": " ",
                "connection_asset_id": "",
                "previewDataInAB": True,
                "registerDataAsset": False,
                "enableFlowAcpControl": True,
                "showPartType": False,
                "showCollType": True,
                "showSortOptions": False,
                "inputName": "Link_2",
                "inputcolProperties": column_properties(),
            },
        }
    ],
    "app_data": {
        "ui_data": {
            "image": "/data-intg/flows/graphics/palette/PxSequentialFile.svg",
            "x_pos": 340.0,
            "y_pos": 110.0,
            "label": "customers_txt",
        }
    },
    "parameters": {
        "combinability": "auto",
        "output_count": 0,
        "input_count": 1,
        "nls_map_name": "UTF-8",
        "execmode": "default_seq",
    },
}

data_set = {
    "op": "PxDataSet",
    "id": N_DS,
    "type": "binding",
    "inputs": [
        {
            "id": I_DS,
            "schema_ref": S_DS,
            "links": [link(N_CPY, P_CPY2, L3, "Link_3")],
            "app_data": {"ui_data": {"label": "inPort", "cardinality": {"min": 0, "max": 1}}},
            "parameters": {
                "dataset": DATASET_NAME,
                "dataAssetName": DATASET_NAME,
                "datasetmode": ">| [ds",
                "registerDataAsset": True,
                "currentOutputLinkType": "PRIMARY",
                "outputAcpShouldHide": False,
                "enableFlowAcpControl": True,
                "showPartType": True,
                "showCollType": False,
                "showSortOptions": False,
                "inputName": "Link_3",
                "inputcolProperties": column_properties(),
            },
        }
    ],
    "app_data": {
        "ui_data": {
            "image": "/data-intg/flows/graphics/palette/PxDataSet.svg",
            "x_pos": 340.0,
            "y_pos": 240.0,
            "label": "customers_ds",
        }
    },
    "parameters": {
        "combinability": "auto",
        "output_count": 0,
        "input_count": 1,
        "execmode": "default_par",
    },
}

flow = {
    "doc_type": "pipeline",
    "version": "3.0",
    "id": FLOW,
    "json_schema": "https://api.dataplatform.ibm.com/schemas/common-pipeline/pipeline-flow/pipeline-flow-v3-schema.json",
    "primary_pipeline": PIPE,
    "pipelines": [
        {
            "id": PIPE,
            "runtime_ref": "pxOsh",
            "nodes": [row_generator, copy_stage, sequential_file, data_set],
            "app_data": {
                "datastage": {"nls_map_name": "", "nls_locale": "OFF"},
                "ui_data": {"comments": []},
            },
        }
    ],
    "schemas": [schema(S_GEN), schema(S_TXT, "Link_1"), schema(S_DS, "Link_1")],
    "runtimes": [{"id": "pxOsh", "name": "pxOsh"}],
    "parameters": {"local_parameters": []},
    "external_paramsets": [],
    "app_data": {
        "datastage": {
            "version": "3.0.5",
            "message_handlers": [],
            "date_format": "",
            "time_format": "",
            "timestamp_format": "",
            "decimal_separator": "",
            "flowRunPriorityQueue": "Medium",
        }
    },
}

json.dump(flow, sys.stdout, indent=1)
sys.stdout.write("\n")
