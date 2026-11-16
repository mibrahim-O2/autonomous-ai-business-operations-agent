"""MCP server exposing the business systems as tools, resources, and prompts."""

from .server import create_business_server

__all__ = ["create_business_server"]
