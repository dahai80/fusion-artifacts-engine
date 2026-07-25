CREATE_ARTIFACT_SCHEMA = {
    "name": "create_artifact",
    "description": "Create an artifact for code, documents, HTML apps, or data files. Use when generating content >30 lines of code or >1500 chars of text.",
    "schema_version": "1.0.0",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Filename or document title"
            },
            "type": {
                "type": "string",
                "enum": ["code", "markdown", "html", "react", "data"],
                "description": "Artifact type"
            },
            "content": {
                "type": "string",
                "description": "Full content of the artifact"
            },
            "summary": {
                "type": "string",
                "description": "Brief description (max 200 chars)"
            }
        },
        "required": ["name", "type", "content"]
    }
}
