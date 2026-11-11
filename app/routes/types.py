from collections.abc import Callable, Generator

from sqlalchemy.orm import Session

from app.config import Settings
from app.paypal import PayPalClient

SessionDependency = Callable[..., Generator[Session, None, None]]
PayPalClientFactory = Callable[[Settings], PayPalClient]
