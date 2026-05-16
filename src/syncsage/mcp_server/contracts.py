from pydantic import BaseModel


class SearchContextRequest(BaseModel):
    knowledge_base: str
    query: str
    mode: str = "hybrid"
    max_results: int = 10
