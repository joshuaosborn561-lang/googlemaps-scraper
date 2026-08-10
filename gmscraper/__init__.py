"""Google Maps -> local-business lead pipeline.

Five stages, each resumable and each with its own CLI subcommand:

    zips      build the US ZIP list (offline, no API calls)
    scrape    Maps Data (RapidAPI), one request per (zip, category)
    enrich    pull homepage/about/team/contact text with html2text
    classify  local Gemma on Ollama confirms the business fits the ICP
    owners    local Gemma pulls the owner's name (+ optional web fallback)
    export    write the CSV

State lives in a single SQLite file so any stage can be killed and restarted
without losing or repeating work.
"""

__version__ = "1.0.0"
