"""Dataiku Python recipe: extract every PDF in a managed folder.

Input: managed folder `pdl_pdfs` (state PDLs and prior authorization criteria). Start file names
with the state code (`OH-unified-pdl-2026-07.pdf`); otherwise the state is read from the title.
Outputs: documents, formulary_rows, drug_access, review_queue, changes

Set USE_LLM to read prose pages with an LLM as well (PDLWATCH_PROVIDER, PDLWATCH_MODEL and the
provider's key in the code environment's variables).
"""

import tempfile
from pathlib import Path

import dataiku

from pdlwatch.extract.document import extract_document
from pdlwatch.pipeline import tables
from pdlwatch.synth.states import STATES

USE_LLM = False

llm = None
if USE_LLM:
    from pdlwatch.agent.provider import get_provider
    from pdlwatch.extract.llm import LLMExtractor

    llm = LLMExtractor(get_provider())

folder = dataiku.Folder("pdl_pdfs")
results = []
with tempfile.TemporaryDirectory() as tmp:
    for path in sorted(folder.list_paths_in_partition()):
        if not path.lower().endswith(".pdf"):
            continue
        local = Path(tmp) / Path(path).name
        with folder.get_download_stream(path) as stream:
            local.write_bytes(stream.read())
        code = local.stem[:2].upper()
        state = code if code in STATES and local.stem[2:3] in {"-", "_", " "} else None
        results.append(extract_document(local, local.stem, state, llm=llm))

out = tables(results, source="Dataiku folder pdl_pdfs")
for name in ("documents", "formulary_rows", "drug_access", "review_queue", "changes"):
    dataiku.Dataset(name).write_with_schema(out[name])
