# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import itertools
import logging
import os
import pathlib
import time
from collections import OrderedDict
from types import TracebackType
from typing import Any, Dict, List, Optional, Union

import yaml

from archai.common.ordered_dict_logger_utils import get_logger


class OrderedDictLogger:
    """Log and save data in a hierarchical YAML structure."""

    def __init__(
        self, source: Optional[str] = None, file_path: Optional[str] = "archai.log.yaml", delay: Optional[float] = 30.0
    ) -> None:
        """Initialize the logger.

        Args:
            source: Source of the logger.
            file_path: File path of the log file.
            delay: Delay between log saves.

        """

        self.logger = get_logger(source or __name__)

        self.file_path = file_path
        self.delay = delay

        self.call_count = 0
        self.timestamp = time.time()

        self.paths = [[""]]
        self.stack = [OrderedDict()]

        if os.path.exists(self.file_path):
            backup_file_path = pathlib.Path(self.file_path)
            backup_file_path.rename(backup_file_path.with_suffix(f".{str(int(time.time()))}.yaml"))

    def __enter__(self) -> OrderedDictLogger:
        """Context manager entry method.

        Returns:
            Instance of logger.

        """

        return self

    def __exit__(self, exc_type: type[BaseException], exc_val: BaseException, exc_tb: TracebackType) -> None:
        """Context manager exit method that pops the current node from the stack."""

        self.popd()

    def __contains__(self, key: str) -> bool:
        """Check if a key is in the current node.

        Args:
            key: key to check.

        Returns:
            `True` if key is in the current node, `False` otherwise.

        """

        return key in self.current_node

    def __len__(self) -> int:
        """Return the number of items in the current node."""

        return len(self.current_node)

    @property
    def root_node(self) -> OrderedDict:
        """Return the root node of the current stack."""

        return self.stack[0]

    @property
    def current_node(self) -> OrderedDict:
        """Return the current node of the current stack.

        Raises:
            RuntimeError: If a `key` stores a scalar value and is trying to store
                new information.

        """

        last_obj = None

        for i, (path, obj) in enumerate(zip(self.paths, self.stack)):
            if obj is None:
                obj = last_obj

                for key in path:
                    if key not in obj:
                        obj[key] = OrderedDict()
                    if not isinstance(obj[key], OrderedDict):
                        raise RuntimeError(f"`{key}` is being used to store a scalar value.")
                    obj = obj[key]

                self.stack[i] = obj

            last_obj = obj

        return self.stack[-1]

    @property
    def current_path(self) -> str:
        """Return the current path of the current stack."""

        return "/".join(itertools.chain.from_iterable(self.paths[1:]))

    def save(self, file_path: Optional[str] = None) -> None:
        """Save the current log data to an output file.

        Args:
            file_path: File path to save the log data to. If `None`,
                defaults to the file path provided during initialization.

        """

        file_path = file_path or self.file_path

        with open(file_path, "w") as f:
            yaml.dump(self.root_node, f)

    def load(self, file_path: str) -> None:
        """Load log data from an input file.

        Args:
            file_path (str): File path to load data from.

        """

        with open(file_path, "r") as f:
            obj = yaml.load(f, Loader=yaml.Loader)
            self.stack = [obj]

    def close(self) -> None:
        """Close the logger."""

        self.save()

        for handler in self.logger.handlers:
            handler.flush()

    def update_key(
        self,
        key: Any,
        value: Any,
        node: Optional[OrderedDict] = None,
        path: Optional[List[str]] = None,
        override_key: Optional[bool] = True,
    ) -> None:
        """Update a key in a node in the current stack.

        Args:
            key: Key to update.
            value: Calue to update the key with.
            node: Node to update the key in.
            path: Path to the node in the current stack.
            override_key: Whether key can be overridden if it's already in current node.

        Raises:
            KeyError: If the key is already being used in the current node and `override_key` is `False`.

        """

        if not override_key and key in self.current_node:
            raise KeyError(f"`{key}` is already being used. Cannot use it again, unless popd() is called.")

        current_node = node or self.current_node
        current_path = path or []

        for p in current_path:
            if p not in current_node:
                current_node[p] = OrderedDict()
            current_node = current_node[p]
        current_node[str(key)] = value

    def update(self, obj: Dict[str, Any], override_key: Optional[bool] = True) -> None:
        """Update the current node with the key-value pairs in the provided object.

        Args:
            obj: Dictionary to update the current node with.
            override_key: Whether key can be overridden if it's already in current node.

        """

        for k, v in obj.items():
            self.update_key(k, v, override_key=override_key)

    def log(
        self, obj: Union[Dict[str, Any], str], level: Optional[int] = None, override_key: Optional[bool] = True
    ) -> None:
        """Log the provided dictionary/string at the specified level.

        Args:
            obj: Object to log.
            level: Logging level.
            override_key: Whether key can be overridden if it's already in current node.

        """

        self.call_count += 1

        if isinstance(obj, dict):
            self.update(obj, override_key=override_key)
            message = ", ".join(f"{k}={v}" for k, v in obj.items())
        else:
            message = obj
            path = {
                logging.INFO: ["messages"],
                logging.DEBUG: ["debugs"],
                logging.WARNING: ["warnings"],
                logging.ERROR: ["errors"],
            }
            self.update_key(self.call_count, message, node=self.root_node, path=path[level], override_key=override_key)

        self.logger.log(msg=self.current_path + " " + message, level=level)

        if time.time() - self.timestamp > self.delay:
            self.save()
            self.timestamp = time.time()

    def info(self, obj: Union[Dict[str, Any], str], override_key: Optional[bool] = True) -> None:
        """Log the provided dictionary/string at the `info` level.

        Args:
            obj: Object to log.
            override_key: Whether key can be overridden if it's already in current node.

        """

        self.log(obj, level=logging.INFO, override_key=override_key)

    def debug(self, obj: Union[Dict[str, Any], str], override_key: Optional[bool] = True) -> None:
        """Log the provided dictionary/string at the `debug` level.

        Args:
            obj: Object to log.
            override_key: Whether key can be overridden if it's already in current node.

        """

        self.log(obj, level=logging.DEBUG, override_key=override_key)

    def warn(self, obj: Union[Dict[str, Any], str], override_key: Optional[bool] = True) -> None:
        """Log the provided dictionary/string at the `warning` level.

        Args:
            obj: Object to log.
            override_key: Whether key can be overridden if it's already in current node.

        """

        self.log(obj, level=logging.WARNING, override_key=override_key)

    def error(self, obj: Union[Dict[str, Any], str], override_key: Optional[bool] = True) -> None:
        """Log the provided dictionary/string at the `error` level.

        Args:
            obj: Object to log.
            override_key: Whether key can be overridden if it's already in current node.

        """

        self.log(obj, level=logging.ERROR, override_key=override_key)

    def pushd(self, *keys: Any) -> OrderedDictLogger:
        """Push the provided keys onto the current path stack.

        Returns:
            Instance of current logger.

        """

        self.paths.append([str(k) for k in keys])
        self.stack.append(None)  # Delays creation of node until it is needed

        return self  # Allows to call __enter__

    def popd(self) -> None:
        """Pop the last path and node off the stack."""

        if len(self.stack) == 1:
            self.warn("Invalid call. No available child in the stack.")
            return

        self.stack.pop()
        self.paths.pop()

    @staticmethod
    def set_instance(instance: OrderedDictLogger) -> None:
        """Set a global logger instance.
        
        Args:
            instance: Instance to be set globally.
            
        """

        global _logger
        _logger = instance

    @staticmethod
    def get_instance() -> OrderedDictLogger:
        """Get a global logger instance.
        
        Returns:
            Global logger.s
            
        """
        
        global _logger
        return _logger


def get_global_logger() -> OrderedDictLogger:
    """
    """
    
    try:
        OrderedDictLogger.get_instance()
    except:
        logger = OrderedDictLogger()
        OrderedDictLogger.set_instance(logger)

    return OrderedDictLogger.get_instance()


# def get_logger(logger:Optional[OrderedDictLogger]=None)->OrderedDictLogger:
#     if logger is not None:
#         return logger
    
#     try:
#         OrderedDictLogger.get_instance()
#     except:
#         logger = OrderedDictLogger()
#         OrderedDictLogger.set_instance(logger)

#     return OrderedDictLogger.get_instance()