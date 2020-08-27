from typing import Any, Callable, Dict, TypeVar, Union

Function = TypeVar('Function', bound=Callable[..., Any])
Payload = Union[None, Dict[str, Any]]
