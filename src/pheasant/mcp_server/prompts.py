PROMPTS = {
    "use_pheasant_for_coding_task": (
        "Before editing, call get_relevant_files, inspect returned provenance, "
        "edit minimally, run checks, then call sync_source."
    ),
    "use_pheasant_for_document_research": (
        "Use search_context first. Prefer chunks with explicit provenance and request "
        "graph neighbors for related material."
    ),
}
