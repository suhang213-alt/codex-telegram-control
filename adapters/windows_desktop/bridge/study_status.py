"""The private corpus dashboard is not part of the public adapter."""
def current():
    return {"available": False, "reason": "integration_not_distributed"}
