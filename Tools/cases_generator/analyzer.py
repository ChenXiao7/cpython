from dataclasses import dataclass, field
import itertools
import lexer
import parser
import re
from typing import Optional

@dataclass
class Properties:
    escaping_calls: dict[lexer.Token, tuple[lexer.Token, lexer.Token]]
    error_with_pop: bool
    error_without_pop: bool
    deopts: bool
    oparg: bool
    jumps: bool
    eval_breaker: bool
    needs_this: bool
    always_exits: bool
    stores_sp: bool
    uses_co_consts: bool
    uses_co_names: bool
    uses_locals: bool
    has_free: bool
    side_exit: bool
    pure: bool
    tier: int | None = None
    oparg_and_1: bool = False
    const_oparg: int = -1
    needs_prev: bool = False

    def dump(self, indent: str) -> None:
        simple_properties = self.__dict__.copy()
        del simple_properties["escaping_calls"]
        text = "escaping_calls:\n"
        for tkns in self.escaping_calls.values():
            text += f"{indent}    {tkns}\n"
        text += ", ".join([f"{key}: {value}" for (key, value) in simple_properties.items()])
        print(indent, text, sep="")

    @staticmethod
    def from_list(properties: list["Properties"]) -> "Properties":
        escaping_calls: dict[lexer.Token, tuple[lexer.Token, lexer.Token]] = {}
        for p in properties:
            escaping_calls.update(p.escaping_calls)
        return Properties(
            escaping_calls=escaping_calls,
            error_with_pop=any(p.error_with_pop for p in properties),
            error_without_pop=any(p.error_without_pop for p in properties),
            deopts=any(p.deopts for p in properties),
            oparg=any(p.oparg for p in properties),
            jumps=any(p.jumps for p in properties),
            eval_breaker=any(p.eval_breaker for p in properties),
            needs_this=any(p.needs_this for p in properties),
            always_exits=any(p.always_exits for p in properties),
            stores_sp=any(p.stores_sp for p in properties),
            uses_co_consts=any(p.uses_co_consts for p in properties),
            uses_co_names=any(p.uses_co_names for p in properties),
            uses_locals=any(p.uses_locals for p in properties),
            has_free=any(p.has_free for p in properties),
            side_exit=any(p.side_exit for p in properties),
            pure=all(p.pure for p in properties),
            needs_prev=any(p.needs_prev for p in properties),
        )

    @property
    def infallible(self) -> bool:
        return not self.error_with_pop and not self.error_without_pop

    @property
    def escapes(self) -> bool:
        return bool(self.escaping_calls)

SKIP_PROPERTIES = Properties(
    escaping_calls={},
    error_with_pop=False,
    error_without_pop=False,
    deopts=False,
    oparg=False,
    jumps=False,
    eval_breaker=False,
    needs_this=False,
    always_exits=False,
    stores_sp=False,
    uses_co_consts=False,
    uses_co_names=False,
    uses_locals=False,
    has_free=False,
    side_exit=False,
    pure=True,
)


@dataclass
class Skip:
    "Unused cache entry"
    size: int

    @property
    def name(self) -> str:
        return f"unused/{self.size}"

    @property
    def properties(self) -> Properties:
        return SKIP_PROPERTIES


class Flush:
    @property
    def properties(self) -> Properties:
        return SKIP_PROPERTIES

    @property
    def name(self) -> str:
        return "flush"

    @property
    def size(self) -> int:
        return 0


@dataclass
class StackItem:
    name: str
    type: str | None
    condition: str | None
    size: str
    peek: bool = False
    used: bool = False

    def __str__(self) -> str:
        cond = f" if ({self.condition})" if self.condition else ""
        size = f"[{self.size}]" if self.size else ""
        type = "" if self.type is None else f"{self.type} "
        return f"{type}{self.name}{size}{cond} {self.peek}"

    def is_array(self) -> bool:
        return self.size != ""

    def get_size(self) -> str:
        return self.size if self.size else "1"


@dataclass
class StackEffect:
    inputs: list[StackItem]
    outputs: list[StackItem]

    def __str__(self) -> str:
        return f"({', '.join([str(i) for i in self.inputs])} -- {', '.join([str(i) for i in self.outputs])})"


@dataclass
class CacheEntry:
    name: str
    size: int

    def __str__(self) -> str:
        return f"{self.name}/{self.size}"


@dataclass
class Uop:
    name: str
    context: parser.Context | None
    annotations: list[str]
    stack: StackEffect
    caches: list[CacheEntry]
    deferred_refs: dict[lexer.Token, str | None]
    output_stores: list[lexer.Token]
    body: list[lexer.Token]
    properties: Properties
    _size: int = -1
    implicitly_created: bool = False
    replicated = 0
    replicates: "Uop | None" = None
    # Size of the instruction(s), only set for uops containing the INSTRUCTION_SIZE macro
    instruction_size: int | None = None

    def dump(self, indent: str) -> None:
        print(
            indent, self.name, ", ".join(self.annotations) if self.annotations else ""
        )
        print(indent, self.stack, ", ".join([str(c) for c in self.caches]))
        self.properties.dump("    " + indent)

    @property
    def size(self) -> int:
        if self._size < 0:
            self._size = sum(c.size for c in self.caches)
        return self._size

    def why_not_viable(self) -> str | None:
        if self.name == "_SAVE_RETURN_OFFSET":
            return None  # Adjusts next_instr, but only in tier 1 code
        if "INSTRUMENTED" in self.name:
            return "is instrumented"
        if "replaced" in self.annotations:
            return "is replaced"
        if self.name in ("INTERPRETER_EXIT", "JUMP_BACKWARD"):
            return "has tier 1 control flow"
        if self.properties.needs_this:
            return "uses the 'this_instr' variable"
        if len([c for c in self.caches if c.name != "unused"]) > 1:
            return "has unused cache entries"
        if self.properties.error_with_pop and self.properties.error_without_pop:
            return "has both popping and not-popping errors"
        return None

    def is_viable(self) -> bool:
        return self.why_not_viable() is None

    def is_super(self) -> bool:
        for tkn in self.body:
            if tkn.kind == "IDENTIFIER" and tkn.text == "oparg1":
                return True
        return False


Part = Uop | Skip | Flush


@dataclass
class Instruction:
    where: lexer.Token
    name: str
    parts: list[Part]
    _properties: Properties | None
    is_target: bool = False
    family: Optional["Family"] = None
    opcode: int = -1

    @property
    def properties(self) -> Properties:
        if self._properties is None:
            self._properties = self._compute_properties()
        return self._properties

    def _compute_properties(self) -> Properties:
        return Properties.from_list([part.properties for part in self.parts])

    def dump(self, indent: str) -> None:
        print(indent, self.name, "=", ", ".join([part.name for part in self.parts]))
        self.properties.dump("    " + indent)

    @property
    def size(self) -> int:
        return 1 + sum(part.size for part in self.parts)

    def is_super(self) -> bool:
        if len(self.parts) != 1:
            return False
        uop = self.parts[0]
        if isinstance(uop, Uop):
            return uop.is_super()
        else:
            return False


@dataclass
class PseudoInstruction:
    name: str
    stack: StackEffect
    targets: list[Instruction]
    as_sequence: bool
    flags: list[str]
    opcode: int = -1

    def dump(self, indent: str) -> None:
        print(indent, self.name, "->", " or ".join([t.name for t in self.targets]))

    @property
    def properties(self) -> Properties:
        return Properties.from_list([i.properties for i in self.targets])


@dataclass
class Family:
    name: str
    size: str
    members: list[Instruction]

    def dump(self, indent: str) -> None:
        print(indent, self.name, "= ", ", ".join([m.name for m in self.members]))


@dataclass
class Analysis:
    instructions: dict[str, Instruction]
    uops: dict[str, Uop]
    families: dict[str, Family]
    pseudos: dict[str, PseudoInstruction]
    opmap: dict[str, int]
    have_arg: int
    min_instrumented: int


def analysis_error(message: str, tkn: lexer.Token) -> SyntaxError:
    # To do -- support file and line output
    # Construct a SyntaxError instance from message and token
    return lexer.make_syntax_error(message, tkn.filename, tkn.line, tkn.column, "")


def override_error(
    name: str,
    context: parser.Context | None,
    prev_context: parser.Context | None,
    token: lexer.Token,
) -> SyntaxError:
    return analysis_error(
        f"Duplicate definition of '{name}' @ {context} "
        f"previous definition @ {prev_context}",
        token,
    )


def convert_stack_item(
    item: parser.StackEffect, replace_op_arg_1: str | None
) -> StackItem:
    cond = item.cond
    if replace_op_arg_1 and OPARG_AND_1.match(item.cond):
        cond = replace_op_arg_1
    return StackItem(item.name, item.type, cond, item.size)


def analyze_stack(
    op: parser.InstDef | parser.Pseudo, replace_op_arg_1: str | None = None
) -> StackEffect:
    inputs: list[StackItem] = [
        convert_stack_item(i, replace_op_arg_1)
        for i in op.inputs
        if isinstance(i, parser.StackEffect)
    ]
    outputs: list[StackItem] = [
        convert_stack_item(i, replace_op_arg_1) for i in op.outputs
    ]
    # Mark variables with matching names at the base of the stack as "peek"
    modified = False
    input_names: dict[str, lexer.Token] = { i.name : i.first_token for i in op.inputs if i.name != "unused" }
    for input, output in itertools.zip_longest(inputs, outputs):
        if output is None:
            pass
        elif input is None:
            if output.name in input_names:
                raise analysis_error(
                    f"Reuse of variable '{output.name}' at different stack location",
                    input_names[output.name])
        elif input.name == output.name:
            if not modified:
                input.peek = output.peek = True
        else:
            modified = True
            if output.name in input_names:
                raise analysis_error(
                    f"Reuse of variable '{output.name}' at different stack location",
                    input_names[output.name])
    if isinstance(op, parser.InstDef):
        output_names = [out.name for out in outputs]
        for input in inputs:
            if (
                variable_used(op, input.name)
                or variable_used(op, "DECREF_INPUTS")
                or (not input.peek and input.name in output_names)
            ):
                input.used = True
        for output in outputs:
            if variable_used(op, output.name):
                output.used = True
    return StackEffect(inputs, outputs)


def analyze_caches(inputs: list[parser.InputEffect]) -> list[CacheEntry]:
    caches: list[parser.CacheEffect] = [
        i for i in inputs if isinstance(i, parser.CacheEffect)
    ]
    for cache in caches:
        if cache.name == "unused":
            raise analysis_error(
                "Unused cache entry in op. Move to enclosing macro.", cache.tokens[0]
            )
    return [CacheEntry(i.name, int(i.size)) for i in caches]


def find_assignment_target(node: parser.InstDef, idx: int) -> list[lexer.Token]:
    """Find the tokens that make up the left-hand side of an assignment"""
    offset = 0
    for tkn in reversed(node.block.tokens[: idx]):
        if tkn.kind in {"SEMI", "LBRACE", "RBRACE"}:
            return node.block.tokens[idx - offset : idx]
        offset += 1
    return []


def find_stores_outputs(node: parser.InstDef) -> list[lexer.Token]:
    res: list[lexer.Token] = []
    outnames = { out.name for out in node.outputs }
    innames = { out.name for out in node.inputs }
    for idx, tkn in enumerate(node.block.tokens):
        if tkn.kind == "AND":
            name = node.block.tokens[idx+1]
            if name.text in outnames:
                res.append(name)
        if tkn.kind != "EQUALS":
            continue
        lhs = find_assignment_target(node, idx)
        assert lhs
        while lhs and lhs[0].kind == "COMMENT":
            lhs = lhs[1:]
        if len(lhs) != 1 or lhs[0].kind != "IDENTIFIER":
            continue
        name = lhs[0]
        if name.text in innames:
            raise analysis_error(f"Cannot assign to input variable '{name.text}'", name)
        if name.text in outnames:
            res.append(name)
    return res

def analyze_deferred_refs(node: parser.InstDef) -> dict[lexer.Token, str | None]:
    """Look for PyStackRef_FromPyObjectNew() calls"""

    def in_frame_push(idx: int) -> bool:
        for tkn in reversed(node.block.tokens[: idx - 1]):
            if tkn.kind in {"SEMI", "LBRACE", "RBRACE"}:
                return False
            if tkn.kind == "IDENTIFIER" and tkn.text == "_PyFrame_PushUnchecked":
                return True
        return False

    refs: dict[lexer.Token, str | None] = {}
    for idx, tkn in enumerate(node.block.tokens):
        if tkn.kind != "IDENTIFIER" or tkn.text != "PyStackRef_FromPyObjectNew":
            continue

        if idx == 0 or node.block.tokens[idx - 1].kind != "EQUALS":
            if in_frame_push(idx):
                # PyStackRef_FromPyObjectNew() is called in _PyFrame_PushUnchecked()
                refs[tkn] = None
                continue
            raise analysis_error("Expected '=' before PyStackRef_FromPyObjectNew", tkn)

        lhs = find_assignment_target(node, idx - 1)
        if len(lhs) == 0:
            raise analysis_error(
                "PyStackRef_FromPyObjectNew() must be assigned to an output", tkn
            )

        if lhs[0].kind == "TIMES" or any(
            t.kind == "ARROW" or t.kind == "LBRACKET" for t in lhs[1:]
        ):
            # Don't handle: *ptr = ..., ptr->field = ..., or ptr[field] = ...
            # Assume that they are visible to the GC.
            refs[tkn] = None
            continue

        if len(lhs) != 1 or lhs[0].kind != "IDENTIFIER":
            raise analysis_error(
                "PyStackRef_FromPyObjectNew() must be assigned to an output", tkn
            )

        name = lhs[0].text
        match = (
            any(var.name == name for var in node.inputs)
            or any(var.name == name for var in node.outputs)
        )
        if not match:
            raise analysis_error(
                f"PyStackRef_FromPyObjectNew() must be assigned to an input or output, not '{name}'",
                tkn,
            )

        refs[tkn] = name

    return refs


def variable_used(node: parser.InstDef, name: str) -> bool:
    """Determine whether a variable with a given name is used in a node."""
    return any(
        token.kind == "IDENTIFIER" and token.text == name for token in node.block.tokens
    )


def oparg_used(node: parser.InstDef) -> bool:
    """Determine whether `oparg` is used in a node."""
    return any(
        token.kind == "IDENTIFIER" and token.text == "oparg" for token in node.tokens
    )


def tier_variable(node: parser.InstDef) -> int | None:
    """Determine whether a tier variable is used in a node."""
    for token in node.tokens:
        if token.kind == "ANNOTATION":
            if token.text == "specializing":
                return 1
            if re.fullmatch(r"tier\d", token.text):
                return int(token.text[-1])
    return None


def has_error_with_pop(op: parser.InstDef) -> bool:
    return (
        variable_used(op, "ERROR_IF")
        or variable_used(op, "pop_1_error")
        or variable_used(op, "exception_unwind")
        or variable_used(op, "resume_with_error")
    )


def has_error_without_pop(op: parser.InstDef) -> bool:
    return (
        variable_used(op, "ERROR_NO_POP")
        or variable_used(op, "pop_1_error")
        or variable_used(op, "exception_unwind")
        or variable_used(op, "resume_with_error")
    )


NON_ESCAPING_FUNCTIONS = (
    "PyCFunction_GET_FLAGS",
    "PyCFunction_GET_FUNCTION",
    "PyCFunction_GET_SELF",
    "PyCell_GetRef",
    "PyCell_New",
    "PyCell_SwapTakeRef",
    "PyExceptionInstance_Class",
    "PyException_GetCause",
    "PyException_GetContext",
    "PyException_GetTraceback",
    "PyFloat_AS_DOUBLE",
    "PyFloat_FromDouble",
    "PyFunction_GET_CODE",
    "PyFunction_GET_GLOBALS",
    "PyList_GET_ITEM",
    "PyList_GET_SIZE",
    "PyList_SET_ITEM",
    "PyLong_AsLong",
    "PyLong_FromLong",
    "PyLong_FromSsize_t",
    "PySlice_New",
    "PyStackRef_AsPyObjectBorrow",
    "PyStackRef_AsPyObjectNew",
    "PyStackRef_AsPyObjectSteal",
    "PyStackRef_CLEAR",
    "PyStackRef_CLOSE",
    "PyStackRef_CLOSE_SPECIALIZED",
    "PyStackRef_DUP",
    "PyStackRef_False",
    "PyStackRef_FromPyObjectImmortal",
    "PyStackRef_FromPyObjectNew",
    "PyStackRef_FromPyObjectSteal",
    "PyStackRef_Is",
    "PyStackRef_IsNull",
    "PyStackRef_None",
    "PyStackRef_TYPE",
    "PyStackRef_True",
    "PyTuple_GET_ITEM",
    "PyTuple_GET_SIZE",
    "PyType_HasFeature",
    "PyUnicode_Append",
    "PyUnicode_Concat",
    "PyUnicode_GET_LENGTH",
    "PyUnicode_READ_CHAR",
    "Py_ARRAY_LENGTH",
    "Py_CLEAR",
    "Py_DECREF",
    "Py_FatalError",
    "Py_INCREF",
    "Py_IS_TYPE",
    "Py_NewRef",
    "Py_REFCNT",
    "Py_SIZE",
    "Py_TYPE",
    "Py_UNREACHABLE",
    "Py_Unicode_GET_LENGTH",
    "Py_XDECREF",
    "_PyCode_CODE",
    "_PyDictValues_AddToInsertionOrder",
    "_PyErr_Occurred",
    "_PyEval_FrameClearAndPop",
    "_PyFloat_FromDouble_ConsumeInputs",
    "_PyFrame_GetCode",
    "_PyFrame_IsIncomplete",
    "_PyFrame_PushUnchecked",
    "_PyFrame_SetStackPointer",
    "_PyFrame_StackPush",
    "_PyFunction_SetVersion",
    "_PyGen_GetGeneratorFromFrame",
    "_PyInterpreterState_GET",
    "_PyList_AppendTakeRef",
    "_PyList_FromStackRefSteal",
    "_PyList_ITEMS",
    "_PyLong_Add",
    "_PyLong_CompactValue",
    "_PyLong_DigitCount",
    "_PyLong_IsCompact",
    "_PyLong_IsNonNegativeCompact",
    "_PyLong_IsZero",
    "_PyLong_Multiply",
    "_PyLong_Subtract",
    "_PyManagedDictPointer_IsValues",
    "_PyObject_GC_IS_TRACKED",
    "_PyObject_GC_MAY_BE_TRACKED",
    "_PyObject_GC_TRACK",
    "_PyObject_GetManagedDict",
    "_PyObject_InlineValues",
    "_PyObject_ManagedDictPointer",
    "_PyThreadState_HasStackSpace",
    "_PyTuple_FromArraySteal",
    "_PyTuple_FromStackRefSteal",
    "_PyTuple_ITEMS",
    "_PyType_HasFeature",
    "_PyType_NewManagedObject",
    "_PyUnicode_Equal",
    "_PyUnicode_JoinArray",
    "_Py_CHECK_EMSCRIPTEN_SIGNALS_PERIODICALLY",
    "_Py_DECREF_NO_DEALLOC",
    "_Py_DECREF_SPECIALIZED",
    "_Py_EnterRecursiveCallTstateUnchecked",
    "_Py_ID",
    "_Py_IsImmortal",
    "_Py_LeaveRecursiveCallPy",
    "_Py_LeaveRecursiveCallTstate",
    "_Py_NewRef",
    "_Py_SINGLETON",
    "_Py_STR",
    "_Py_atomic_load_uintptr_relaxed",
    "_Py_set_eval_breaker_bit",
    "advance_backoff_counter",
    "assert",
    "backoff_counter_triggers",
    "initial_temperature_backoff_counter",
    "maybe_lltrace_resume_frame",
    "restart_backoff_counter",
)

def find_stmt_start(node: parser.InstDef, idx: int) -> lexer.Token:
    assert idx < len(node.block.tokens)
    while True:
        tkn = node.block.tokens[idx-1]
        if tkn.kind in {"SEMI", "LBRACE", "RBRACE", "CMACRO"}:
            break
        idx -= 1
        assert idx > 0
    while node.block.tokens[idx].kind == "COMMENT":
        idx += 1
    return node.block.tokens[idx]


def find_stmt_end(node: parser.InstDef, idx: int) -> lexer.Token:
    assert idx < len(node.block.tokens)
    while True:
        idx += 1
        tkn = node.block.tokens[idx]
        if tkn.kind == "SEMI":
            return node.block.tokens[idx+1]

def check_escaping_calls(instr: parser.InstDef, escapes: dict[lexer.Token, tuple[lexer.Token, lexer.Token]]) -> None:
    calls = {escapes[t][0] for t in escapes}
    in_if = 0
    tkn_iter = iter(instr.block.tokens)
    for tkn in tkn_iter:
        if tkn.kind == "IF":
            next(tkn_iter)
            in_if = 1
        if tkn.kind == "IDENTIFIER" and tkn.text in ("DEOPT_IF", "ERROR_IF"):
            next(tkn_iter)
            in_if = 1
        elif tkn.kind == "LPAREN" and in_if:
            in_if += 1
        elif tkn.kind == "RPAREN":
            if in_if:
                in_if -= 1
        elif tkn in calls and in_if:
            raise analysis_error(f"Escaping call '{tkn.text} in condition", tkn)

def find_escaping_api_calls(instr: parser.InstDef) -> dict[lexer.Token, tuple[lexer.Token, lexer.Token]]:
    result: dict[lexer.Token, tuple[lexer.Token, lexer.Token]] = {}
    tokens = instr.block.tokens
    for idx, tkn in enumerate(tokens):
        try:
            next_tkn = tokens[idx+1]
        except IndexError:
            break
        if tkn.kind == "SWITCH":
            raise analysis_error(f"switch statements are not supported due to their complex flow control. Sorry.", tkn)
        if next_tkn.kind != lexer.LPAREN:
            continue
        if tkn.kind == lexer.IDENTIFIER:
            if tkn.text.upper() == tkn.text:
                # simple macro
                continue
            #if not tkn.text.startswith(("Py", "_Py", "monitor")):
            #    continue
            if tkn.text.startswith(("sym_", "optimize_")):
                # Optimize functions
                continue
            if tkn.text.endswith("Check"):
                continue
            if tkn.text.startswith("Py_Is"):
                continue
            if tkn.text.endswith("CheckExact"):
                continue
            if tkn.text in NON_ESCAPING_FUNCTIONS:
                continue
        elif tkn.kind == "RPAREN":
            prev = tokens[idx-1]
            if prev.text.endswith("_t") or prev.text == "*" or prev.text == "int":
                #cast
                continue
        elif tkn.kind != "RBRACKET":
            continue
        start = find_stmt_start(instr, idx)
        end = find_stmt_end(instr, idx)
        result[start] = tkn, end
    check_escaping_calls(instr, result)
    return result


EXITS = {
    "DISPATCH",
    "GO_TO_INSTRUCTION",
    "Py_UNREACHABLE",
    "DISPATCH_INLINED",
    "DISPATCH_GOTO",
}


def always_exits(op: parser.InstDef) -> bool:
    depth = 0
    tkn_iter = iter(op.tokens)
    for tkn in tkn_iter:
        if tkn.kind == "LBRACE":
            depth += 1
        elif tkn.kind == "RBRACE":
            depth -= 1
        elif depth > 1:
            continue
        elif tkn.kind == "GOTO" or tkn.kind == "RETURN":
            return True
        elif tkn.kind == "KEYWORD":
            if tkn.text in EXITS:
                return True
        elif tkn.kind == "IDENTIFIER":
            if tkn.text in EXITS:
                return True
            if tkn.text == "DEOPT_IF" or tkn.text == "ERROR_IF":
                next(tkn_iter)  # '('
                t = next(tkn_iter)
                if t.text in ("true", "1"):
                    return True
    return False


def stack_effect_only_peeks(instr: parser.InstDef) -> bool:
    stack_inputs = [s for s in instr.inputs if not isinstance(s, parser.CacheEffect)]
    if len(stack_inputs) != len(instr.outputs):
        return False
    if len(stack_inputs) == 0:
        return False
    if any(s.cond for s in stack_inputs) or any(s.cond for s in instr.outputs):
        return False
    return all(
        (s.name == other.name and s.type == other.type and s.size == other.size)
        for s, other in zip(stack_inputs, instr.outputs)
    )


OPARG_AND_1 = re.compile("\\(*oparg *& *1")


def effect_depends_on_oparg_1(op: parser.InstDef) -> bool:
    for effect in op.inputs:
        if isinstance(effect, parser.CacheEffect):
            continue
        if not effect.cond:
            continue
        if OPARG_AND_1.match(effect.cond):
            return True
    for effect in op.outputs:
        if not effect.cond:
            continue
        if OPARG_AND_1.match(effect.cond):
            return True
    return False


def compute_properties(op: parser.InstDef) -> Properties:
    escaping_calls = find_escaping_api_calls(op)
    has_free = (
        variable_used(op, "PyCell_New")
        or variable_used(op, "PyCell_GetRef")
        or variable_used(op, "PyCell_SetTakeRef")
        or variable_used(op, "PyCell_SwapTakeRef")
    )
    deopts_if = variable_used(op, "DEOPT_IF")
    exits_if = variable_used(op, "EXIT_IF")
    if deopts_if and exits_if:
        tkn = op.tokens[0]
        raise lexer.make_syntax_error(
            "Op cannot contain both EXIT_IF and DEOPT_IF",
            tkn.filename,
            tkn.line,
            tkn.column,
            op.name,
        )
    error_with_pop = has_error_with_pop(op)
    error_without_pop = has_error_without_pop(op)
    return Properties(
        escaping_calls=escaping_calls,
        error_with_pop=error_with_pop,
        error_without_pop=error_without_pop,
        deopts=deopts_if,
        side_exit=exits_if,
        oparg=oparg_used(op),
        jumps=variable_used(op, "JUMPBY"),
        eval_breaker="CHECK_PERIODIC" in op.name,
        needs_this=variable_used(op, "this_instr"),
        always_exits=always_exits(op),
        stores_sp=variable_used(op, "SYNC_SP"),
        uses_co_consts=variable_used(op, "FRAME_CO_CONSTS"),
        uses_co_names=variable_used(op, "FRAME_CO_NAMES"),
        uses_locals=(variable_used(op, "GETLOCAL") or variable_used(op, "SETLOCAL"))
        and not has_free,
        has_free=has_free,
        pure="pure" in op.annotations,
        tier=tier_variable(op),
        needs_prev=variable_used(op, "prev_instr"),
    )


def make_uop(
    name: str,
    op: parser.InstDef,
    inputs: list[parser.InputEffect],
    uops: dict[str, Uop],
) -> Uop:
    result = Uop(
        name=name,
        context=op.context,
        annotations=op.annotations,
        stack=analyze_stack(op),
        caches=analyze_caches(inputs),
        deferred_refs=analyze_deferred_refs(op),
        output_stores=find_stores_outputs(op),
        body=op.block.tokens,
        properties=compute_properties(op),
    )
    if effect_depends_on_oparg_1(op) and "split" in op.annotations:
        result.properties.oparg_and_1 = True
        for bit in ("0", "1"):
            name_x = name + "_" + bit
            properties = compute_properties(op)
            if properties.oparg:
                # May not need oparg anymore
                properties.oparg = any(
                    token.text == "oparg" for token in op.block.tokens
                )
            rep = Uop(
                name=name_x,
                context=op.context,
                annotations=op.annotations,
                stack=analyze_stack(op, bit),
                caches=analyze_caches(inputs),
                deferred_refs=analyze_deferred_refs(op),
                output_stores=find_stores_outputs(op),
                body=op.block.tokens,
                properties=properties,
            )
            rep.replicates = result
            uops[name_x] = rep
    for anno in op.annotations:
        if anno.startswith("replicate"):
            result.replicated = int(anno[10:-1])
            break
    else:
        return result
    for oparg in range(result.replicated):
        name_x = name + "_" + str(oparg)
        properties = compute_properties(op)
        properties.oparg = False
        properties.const_oparg = oparg
        rep = Uop(
            name=name_x,
            context=op.context,
            annotations=op.annotations,
            stack=analyze_stack(op),
            caches=analyze_caches(inputs),
            deferred_refs=analyze_deferred_refs(op),
            output_stores=find_stores_outputs(op),
            body=op.block.tokens,
            properties=properties,
        )
        rep.replicates = result
        uops[name_x] = rep

    return result


def add_op(op: parser.InstDef, uops: dict[str, Uop]) -> None:
    assert op.kind == "op"
    if op.name in uops:
        if "override" not in op.annotations:
            raise override_error(
                op.name, op.context, uops[op.name].context, op.tokens[0]
            )
    uops[op.name] = make_uop(op.name, op, op.inputs, uops)


def add_instruction(
    where: lexer.Token,
    name: str,
    parts: list[Part],
    instructions: dict[str, Instruction],
) -> None:
    instructions[name] = Instruction(where, name, parts, None)


def desugar_inst(
    inst: parser.InstDef, instructions: dict[str, Instruction], uops: dict[str, Uop]
) -> None:
    assert inst.kind == "inst"
    name = inst.name
    op_inputs: list[parser.InputEffect] = []
    parts: list[Part] = []
    uop_index = -1
    # Move unused cache entries to the Instruction, removing them from the Uop.
    for input in inst.inputs:
        if isinstance(input, parser.CacheEffect) and input.name == "unused":
            parts.append(Skip(input.size))
        else:
            op_inputs.append(input)
            if uop_index < 0:
                uop_index = len(parts)
                # Place holder for the uop.
                parts.append(Skip(0))
    uop = make_uop("_" + inst.name, inst, op_inputs, uops)
    uop.implicitly_created = True
    uops[inst.name] = uop
    if uop_index < 0:
        parts.append(uop)
    else:
        parts[uop_index] = uop
    add_instruction(inst.first_token, name, parts, instructions)


def add_macro(
    macro: parser.Macro, instructions: dict[str, Instruction], uops: dict[str, Uop]
) -> None:
    parts: list[Part] = []
    for part in macro.uops:
        match part:
            case parser.OpName():
                if part.name == "flush":
                    parts.append(Flush())
                else:
                    if part.name not in uops:
                        raise analysis_error(
                            f"No Uop named {part.name}", macro.tokens[0]
                        )
                    parts.append(uops[part.name])
            case parser.CacheEffect():
                parts.append(Skip(part.size))
            case _:
                assert False
    assert parts
    add_instruction(macro.first_token, macro.name, parts, instructions)


def add_family(
    pfamily: parser.Family,
    instructions: dict[str, Instruction],
    families: dict[str, Family],
) -> None:
    family = Family(
        pfamily.name,
        pfamily.size,
        [instructions[member_name] for member_name in pfamily.members],
    )
    for member in family.members:
        member.family = family
    # The head of the family is an implicit jump target for DEOPTs
    instructions[family.name].is_target = True
    families[family.name] = family


def add_pseudo(
    pseudo: parser.Pseudo,
    instructions: dict[str, Instruction],
    pseudos: dict[str, PseudoInstruction],
) -> None:
    pseudos[pseudo.name] = PseudoInstruction(
        pseudo.name,
        analyze_stack(pseudo),
        [instructions[target] for target in pseudo.targets],
        pseudo.as_sequence,
        pseudo.flags,
    )


def assign_opcodes(
    instructions: dict[str, Instruction],
    families: dict[str, Family],
    pseudos: dict[str, PseudoInstruction],
) -> tuple[dict[str, int], int, int]:
    """Assigns opcodes, then returns the opmap,
    have_arg and min_instrumented values"""
    instmap: dict[str, int] = {}

    # 0 is reserved for cache entries. This helps debugging.
    instmap["CACHE"] = 0

    # 17 is reserved as it is the initial value for the specializing counter.
    # This helps catch cases where we attempt to execute a cache.
    instmap["RESERVED"] = 17

    # 149 is RESUME - it is hard coded as such in Tools/build/deepfreeze.py
    instmap["RESUME"] = 149

    # This is an historical oddity.
    instmap["BINARY_OP_INPLACE_ADD_UNICODE"] = 3

    instmap["INSTRUMENTED_LINE"] = 254
    instmap["ENTER_EXECUTOR"] = 255

    instrumented = [name for name in instructions if name.startswith("INSTRUMENTED")]

    specialized: set[str] = set()
    no_arg: list[str] = []
    has_arg: list[str] = []

    for family in families.values():
        specialized.update(inst.name for inst in family.members)

    for inst in instructions.values():
        name = inst.name
        if name in specialized:
            continue
        if name in instrumented:
            continue
        if inst.properties.oparg:
            has_arg.append(name)
        else:
            no_arg.append(name)

    # Specialized ops appear in their own section
    # Instrumented opcodes are at the end of the valid range
    min_internal = 150
    min_instrumented = 254 - (len(instrumented) - 1)
    assert min_internal + len(specialized) < min_instrumented

    next_opcode = 1

    def add_instruction(name: str) -> None:
        nonlocal next_opcode
        if name in instmap:
            return  # Pre-defined name
        while next_opcode in instmap.values():
            next_opcode += 1
        instmap[name] = next_opcode
        next_opcode += 1

    for name in sorted(no_arg):
        add_instruction(name)
    for name in sorted(has_arg):
        add_instruction(name)
    # For compatibility
    next_opcode = min_internal
    for name in sorted(specialized):
        add_instruction(name)
    next_opcode = min_instrumented
    for name in instrumented:
        add_instruction(name)

    for name in instructions:
        instructions[name].opcode = instmap[name]

    for op, name in enumerate(sorted(pseudos), 256):
        instmap[name] = op
        pseudos[name].opcode = op

    return instmap, len(no_arg), min_instrumented


def get_instruction_size_for_uop(instructions: dict[str, Instruction], uop: Uop) -> int | None:
    """Return the size of the instruction that contains the given uop or
    `None` if the uop does not contains the `INSTRUCTION_SIZE` macro.

    If there is more than one instruction that contains the uop,
    ensure that they all have the same size.
    """
    for tkn in uop.body:
          if tkn.text == "INSTRUCTION_SIZE":
               break
    else:
        return None

    size = None
    for inst in instructions.values():
        if uop in inst.parts:
            if size is None:
                size = inst.size
            if size != inst.size:
                raise analysis_error(
                    "All instructions containing a uop with the `INSTRUCTION_SIZE` macro "
                    f"must have the same size: {size} != {inst.size}",
                    tkn
                )
    if size is None:
        raise analysis_error(f"No instruction containing the uop '{uop.name}' was found", tkn)
    return size


def analyze_forest(forest: list[parser.AstNode]) -> Analysis:
    instructions: dict[str, Instruction] = {}
    uops: dict[str, Uop] = {}
    families: dict[str, Family] = {}
    pseudos: dict[str, PseudoInstruction] = {}
    for node in forest:
        match node:
            case parser.InstDef(name):
                if node.kind == "inst":
                    desugar_inst(node, instructions, uops)
                else:
                    assert node.kind == "op"
                    add_op(node, uops)
            case parser.Macro():
                pass
            case parser.Family():
                pass
            case parser.Pseudo():
                pass
            case _:
                assert False
    for node in forest:
        if isinstance(node, parser.Macro):
            add_macro(node, instructions, uops)
    for node in forest:
        match node:
            case parser.Family():
                add_family(node, instructions, families)
            case parser.Pseudo():
                add_pseudo(node, instructions, pseudos)
            case _:
                pass
    for uop in uops.values():
        tkn_iter = iter(uop.body)
        for tkn in tkn_iter:
            if tkn.kind == "IDENTIFIER" and tkn.text == "GO_TO_INSTRUCTION":
                if next(tkn_iter).kind != "LPAREN":
                    continue
                target = next(tkn_iter)
                if target.kind != "IDENTIFIER":
                    continue
                if target.text in instructions:
                    instructions[target.text].is_target = True
    for uop in uops.values():
        uop.instruction_size = get_instruction_size_for_uop(instructions, uop)
    # Special case BINARY_OP_INPLACE_ADD_UNICODE
    # BINARY_OP_INPLACE_ADD_UNICODE is not a normal family member,
    # as it is the wrong size, but we need it to maintain an
    # historical optimization.
    if "BINARY_OP_INPLACE_ADD_UNICODE" in instructions:
        inst = instructions["BINARY_OP_INPLACE_ADD_UNICODE"]
        inst.family = families["BINARY_OP"]
        families["BINARY_OP"].members.append(inst)
    opmap, first_arg, min_instrumented = assign_opcodes(instructions, families, pseudos)
    return Analysis(
        instructions, uops, families, pseudos, opmap, first_arg, min_instrumented
    )


def analyze_files(filenames: list[str]) -> Analysis:
    return analyze_forest(parser.parse_files(filenames))


def dump_analysis(analysis: Analysis) -> None:
    print("Uops:")
    for u in analysis.uops.values():
        u.dump("    ")
    print("Instructions:")
    for i in analysis.instructions.values():
        i.dump("    ")
    print("Families:")
    for f in analysis.families.values():
        f.dump("    ")
    print("Pseudos:")
    for p in analysis.pseudos.values():
        p.dump("    ")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("No input")
    else:
        filenames = sys.argv[1:]
        dump_analysis(analyze_files(filenames))