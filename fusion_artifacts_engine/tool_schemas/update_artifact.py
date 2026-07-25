UPDATE_ARTIFACT_SCHEMA = {
    "name": "update_artifact",
    "description": "Update an existing artifact with new content, creating a new version.",
    "schema_version": "1.0.0",
    "input_schema": {
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "The artifact ID (art_xxx)"
            },
            "content": {
                "type": "string",
                "description": "Updated full content"
            },
            "change_log": {
                "type": "string",
                "description": "What changed in this version"
            }
        },
        "required": ["artifact_id", "content"]
    }
}
