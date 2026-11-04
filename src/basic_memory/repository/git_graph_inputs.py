"""Conservative classification of changes that cannot affect other note targets."""

from collections.abc import Mapping
from uuid import UUID

from basic_memory.file_utils import parse_frontmatter


def git_graph_identities_unchanged(
    previous_files: Mapping[str, str],
    previous_ids: Mapping[str, UUID],
    files: Mapping[str, str],
    note_ids: Mapping[str, UUID],
) -> bool:
    """Only body changes permit reusing unchanged sources' strict link resolution.

    Keep every frontmatter field conservative, including extraction provenance
    and semantic timestamps. A structural change requires a full graph pass.
    """
    return previous_ids == note_ids and all(
        previous_files[path] == content
        or parse_frontmatter(previous_files[path]) == parse_frontmatter(content)
        for path, content in files.items()
    )
