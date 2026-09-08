from dataclasses import dataclass


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    retry_after: int | None = None

    def payload(self, request_id: str) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "requestId": request_id,
        }
        if self.retry_after is not None:
            result["retryAfter"] = self.retry_after
        return result
