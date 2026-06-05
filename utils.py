import json
import globals
from sqlalchemy import create_engine,text,bindparam
from sqlalchemy.orm import  Session
import pandas as pd

def memdata(observed:dict,store:dict):
    globals.logger.debug(f'mem:{store}')
    
    jsonquery=store['jsonquery']

    if not bool(jsonquery):
        return globals.refdata[store['object']]
    else:
        return  None #jsonLogic(jsonquery, globals.refdata[store['object']], operations=DRONA)

def execute_query_with_params(session, query, param_dict):
    processed_query = query
    bind_params = []

    for key, value in param_dict.items():
        bind_param_key = key.replace('.', '_')
        processed_query = processed_query.replace(f':{key}', f':{bind_param_key}')

        # If the value is a list/tuple, use expanding=True
        if isinstance(value, (list, tuple)):
            bind_params.append(bindparam(bind_param_key, expanding=True))
        else:
            bind_params.append(bindparam(bind_param_key))

    stmt = text(processed_query).bindparams(*bind_params)
    result = session.execute(stmt, {key.replace('.', '_'): value for key, value in param_dict.items()})
    return result

def execute_query(session, query, param_dict):
    processed_query = query


    for key, value in param_dict.items():
        
        processed_query = processed_query.replace(f':{key}', f'{value}')
        

    result = session.execute(text(processed_query))
    return result

def build_query(agent_data,store_params):
    where_clause=""
    for key in store_params:
        opr= key.get('operator','')
        data=get_var(agent_data,key['valueField'])
        if key.get('type','') == 'list':
            result = ','.join(f'"{item}"' for item in data)

            where_clause=where_clause+" "+ opr+" " + key['name'] +" in (" +result+")"
        else:    
            where_clause=where_clause+" "+ opr+" " + key['name'] +"='" +data+"'"
       
def dbdata(agent_data:dict,store:dict):
    globals.logger.debug(f'db:{store}')
    globals.logger.debug(f'agent_data:{agent_data}')
    with Session(globals.dbs[store['conn']]['engine']) as session:
            
        column_values={}
         
        for key in store['params']:

            column_values[key['name']]=get_var(agent_data,key['valueField'])
        query = store['query']

        result = execute_query_with_params(session, query, column_values)
        if store.get("noofrows","one")=="one":
            row = result.fetchone()
            if row:
                return row[0]
        else:
            rows = result.fetchall()
            keys = result.keys()
            result_list = [dict(zip(keys, row)) for row in rows]
            return result_list
    return None


      
def rocksdata(observed:dict,store:dict):
    globals.logger.debug(f'rocks:{store}')
    
    key_raw_value=get_var(observed,store['key'])
    if key_raw_value is not None:
        if store['keytype']=='int':
            key_value=str(int(key_raw_value))
        else:
            key_value=key_raw_value
        
        v = globals.rocksdb_db[store['object']].get(key_value.encode())
        if v:
            return json.loads(v)
        else:
            return None
    else:
        return None    
    

def redisdata(observed:dict,store:dict):
    globals.logger.debug(f'redisdata:{store}')
    values={}
    for key in store['key']:
        val=get_var(observed,key['val'])
        if val is None:
            values[key['name']]= None
        else:    
            if key['keytype']=='int':
                values[key['name']]=str(int(val))           
            else:
                values[key['name']]=val
    #print(values)    
    #print(store['redis_key'])
    redis_key=store['redis_key'].format(**values)
    v=globals.rs[store['object']].get(redis_key)
    if v:
        return json.loads(v)
    else:
        return None
     

def apidata(observed:dict,store:dict):
    globals.logger.debug(f'api:{store}')    
    return 'x'

def get_var(data, var_name, not_found=None):
    """Gets variable value from data dictionary, supports dot notation and '*' for lists."""
    try:
        parts = str(var_name).split('.')
        for i, key in enumerate(parts):
            if key == '*':
                if not isinstance(data, list):
                    return not_found
                rest = '.'.join(parts[i + 1:])
                return [get_var(item, rest, not_found) for item in data]
            try:
                data = data[key]
            except TypeError:
                data = data[int(key)]
    except (KeyError, TypeError, ValueError):
        return not_found
    else:
        return data

def apply_chunking(text, chunking_config: dict) -> list:
    """
    Splits text according to the chunking strategy in chunking_config.

    Strategies:
      single    — returns the whole string as one element (no splitting).
      token     — TokenTextSplitter; chunk_size and overlap required.
      character — RecursiveCharacterTextSplitter; chunk_size and overlap required.
      page      — text must already be a list of page strings (pre-split upstream).
      row       — text must already be a list of row strings (pre-serialised upstream).

    Returns a list[str].
    """
    from langchain.text_splitter import (
        TokenTextSplitter,
        RecursiveCharacterTextSplitter,
    )

    strategy   = chunking_config.get('strategy', 'single')
    chunk_size = chunking_config.get('chunk_size')
    overlap    = chunking_config.get('overlap') or 0

    if strategy == 'single':
        return [text] if isinstance(text, str) else list(text)

    elif strategy == 'token':
        if not chunk_size:
            raise ValueError("chunk_size is required for the 'token' chunking strategy")
        splitter = TokenTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
        )
        return splitter.split_text(text)

    elif strategy == 'character':
        if not chunk_size:
            raise ValueError("chunk_size is required for the 'character' chunking strategy")
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        return splitter.split_text(text)

    elif strategy == 'page':
        # Pre-split upstream; text arrives as a list of page strings.
        if isinstance(text, list):
            return [p for p in text if p and p.strip()]
        return [text]   # fallback: treat plain string as a single page

    elif strategy == 'row':
        # Pre-serialised upstream; text arrives as a list of row strings.
        if isinstance(text, list):
            return [r for r in text if r and r.strip()]
        return [text]

    else:
        raise ValueError(
            f"Unknown chunking strategy: '{strategy}'. "
            "Valid values: single, token, character, page, row"
        )


# ─── DMS-003 helpers ──────────────────────────────────────────────────────────

import re as _re
import time as _time

INJECTION_PATTERNS = [
    r'ignore (previous|all|above) instructions',
    r'you are now',
    r'act as',
    r'disregard your',
    r'system prompt',
    r'<\|.*\|>',
]

def check_injection(text: str) -> bool:
    """
    Returns True if the text contains prompt-injection patterns.
    Documents are NOT blocked — callers should set injection_flag in metadata.
    """
    text_lower = text.lower() if isinstance(text, str) else ''
    return any(_re.search(p, text_lower) for p in INJECTION_PATTERNS)


def embed_with_retry(texts: list, embedder, max_retries: int = 3) -> list:
    """
    Calls embedder.embed_documents(texts) with exponential-backoff retry.
    Raises on final failure.
    """
    for attempt in range(max_retries):
        try:
            return embedder.embed_documents(texts)
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            globals.logger.warning(
                "Embedding attempt %d failed, retrying in %ds: %s",
                attempt + 1, wait, exc
            )
            _time.sleep(wait)


def extract_pdf_text(file_bytes: bytes) -> str:
    """Extracts plain text from a native (non-scanned) PDF."""
    from pypdf import PdfReader
    import io
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = [page.extract_text() or '' for page in reader.pages]
    return '\n'.join(pages)


def extract_docx_text(file_bytes: bytes) -> str:
    """Extracts plain text from a .docx file."""
    from docx import Document as DocxDocument
    import io
    doc = DocxDocument(io.BytesIO(file_bytes))
    return '\n'.join(para.text for para in doc.paragraphs if para.text.strip())


def extract_csv_rows(file_bytes: bytes) -> list:
    """
    Parses a CSV and returns one serialised string per data row:
    ["col1: val, col2: val", ...]
    """
    import csv, io
    text = file_bytes.decode('utf-8', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    return [
        ', '.join(f"{k}: {v}" for k, v in row.items() if v is not None)
        for row in reader
    ]


def extract_json_records(file_bytes: bytes) -> list:
    """
    Parses a JSON file.  Returns one serialised string per element if the
    top-level value is an array; returns a single-element list otherwise.
    """
    import json as _json
    data = _json.loads(file_bytes.decode('utf-8', errors='replace'))
    if isinstance(data, list):
        return [
            _json.dumps(item) if isinstance(item, (dict, list)) else str(item)
            for item in data
        ]
    return [_json.dumps(data)]


# ──────────────────────────────────────────────────────────────────────────────

def flatten_dict(d, parent_key='', sep='_'):
    items = []
    for k, v in d.items():
        new_key = f'{parent_key}{sep}{k}' if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)