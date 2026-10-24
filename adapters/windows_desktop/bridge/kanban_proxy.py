"""No project-specific board backend is distributed with this adapter."""
class KanbanError(Exception):
    def __init__(self):
        self.status = 404
        self.payload = {"error": "Board integration unavailable", "code": "not_available"}
def get_proxy():
    raise KanbanError()
