from __future__ import annotations

import ast
import logging
import math
import operator
from typing import Any

logger = logging.getLogger(__name__)

# Safety: prevent stack overflow from deeply nested expressions
_MAX_RECURSION_DEPTH = 50

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "Evaluate a mathematical expression. Supports: "
            "basic arithmetic (+, -, *, /, **, %), "
            "trigonometry (sin, cos, tan), "
            "logarithms (log, log2, log10), "
            "constants (pi, e), "
            "rounding (round, floor, ceil, abs), "
            "square root (sqrt), "
            "factorial, and more."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The mathematical expression to evaluate, e.g., '2 + 2', 'sin(pi/4)', 'sqrt(144)'",
                },
            },
            "required": ["expression"],
        },
    },
}

# Safe math operations
_SAFE_OPERATORS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_NAMES: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "inf": math.inf,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "sqrt": math.sqrt,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "abs": abs,
    "round": round,
    "factorial": math.factorial,
    "degrees": math.degrees,
    "radians": math.radians,
    "gcd": math.gcd,
    "pow": pow,
}


def _safe_eval(node: ast.AST, _depth: int = 0) -> Any:
    """Safely evaluate an AST node without using eval()."""
    if _depth > _MAX_RECURSION_DEPTH:
        raise ValueError("Expression too deeply nested — recursion limit exceeded.")
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, _depth + 1)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id in _SAFE_NAMES:
            return _SAFE_NAMES[node.id]
        raise ValueError(f"Unknown identifier: '{node.id}'")

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        left = _safe_eval(node.left, _depth + 1)
        right = _safe_eval(node.right, _depth + 1)
        return _SAFE_OPERATORS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = _safe_eval(node.operand, _depth + 1)
        return _SAFE_OPERATORS[op_type](operand)

    if isinstance(node, ast.Call):
        func_name = node.func.id if isinstance(node.func, ast.Name) else None
        if func_name not in _SAFE_NAMES:
            raise ValueError(f"Unknown function: '{func_name}'")
        args = [_safe_eval(arg, _depth + 1) for arg in node.args]
        return _SAFE_NAMES[func_name](*args)

    if isinstance(node, ast.List):
        return [_safe_eval(el, _depth + 1) for el in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_safe_eval(el, _depth + 1) for el in node.elts)

    raise ValueError(f"Unsupported syntax: {type(node).__name__}")


async def calculate(expression: str) -> str:
    """Safely evaluate a mathematical expression."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _safe_eval(tree.body)
        # Format nicely
        if isinstance(result, float):
            formatted = f"{result:g}" if result == int(result) else f"{result:.10g}"
        else:
            formatted = str(result)
        return f"Result: {expression} = {formatted}"
    except ValueError as e:
        return f"Calculation error: {e}"
    except SyntaxError:
        return (
            f"Invalid expression syntax: '{expression}'. "
            "Use valid mathematical notation, e.g., '2 + 2', 'sin(pi/4)', '5 ** 3'"
        )
    except Exception as e:
        logger.error("Calculator error: %s", e)
        return f"Calculation error: {e}"