"""Notification service stubs. Implementations added in a future phase."""


def send_email(subject: str, body: str, to_address: str) -> None:
    pass  # TODO


def send_telegram(message: str, chat_id: str) -> None:
    pass  # TODO


def notify_upcoming_deadline(deadline: dict) -> None:
    pass  # TODO


def notify_case_changed(case: dict, change_description: str) -> None:
    pass  # TODO
