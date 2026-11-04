## CloudServer-Support App Architecture

```

CloudServe-Support/
├── src/
│   └── cloudserve_support/
│       ├── __init__.py
│       ├── app/
│       │   ├── __init__.py
│       │   ├── main.py          * FastAPI, POST /tickets
│       │   └── schemas.py       * Pydantic Ticket and Response models
│       ├── pipeline/
│       │   ├── __init__.py
│       │   ├── ingest.py        * cleaning, validation, name masking
│       │   └── graph.py         * LangGraph flow 
│       └── harness/
│           ├── __init__.py
│           └── __main__.py      --input / --output runner
├── data/                        * tickets,docs, groundtruth JSON 
├── prompts/                     stays at root (versioned prompt files)
├── tests/                       stays at root
├── .env.example
├── .gitignore
├── .python-version
├── pyproject.toml
├── README.md
└── uv.lock

```