import datetime
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Union
from unittest import mock

import pandas as pd
import pydantic
import pytest

import mlflow
from mlflow.exceptions import MlflowException
from mlflow.models.signature import _extract_type_hints, infer_signature
from mlflow.pyfunc.utils import pyfunc
from mlflow.types.schema import AnyType, Array, ColSpec, DataType, Map, Object, Property, Schema
from mlflow.types.type_hints import PYDANTIC_V1_OR_OLDER, _is_pydantic_type_hint


class CustomExample(pydantic.BaseModel):
    long_field: int
    str_field: str
    bool_field: bool
    double_field: float
    binary_field: bytes
    datetime_field: datetime.datetime
    any_field: Any
    optional_str: Optional[str] = None


class Message(pydantic.BaseModel):
    role: str
    content: str


class CustomExample2(pydantic.BaseModel):
    custom_field: dict[str, Any]
    messages: list[Message]
    optional_int: Optional[int] = None


@pytest.mark.parametrize(
    ("type_hint", "expected_schema", "input_example"),
    [
        # scalars
        (int, Schema([ColSpec(type=DataType.long)]), 123),
        (str, Schema([ColSpec(type=DataType.string)]), "string"),
        (bool, Schema([ColSpec(type=DataType.boolean)]), True),
        (float, Schema([ColSpec(type=DataType.double)]), 1.23),
        (bytes, Schema([ColSpec(type=DataType.binary)]), b"bytes"),
        (datetime.datetime, Schema([ColSpec(type=DataType.datetime)]), datetime.datetime.now()),
        # lists
        (list[str], Schema([ColSpec(type=Array(DataType.string))]), ["a", "b"]),
        (List[str], Schema([ColSpec(type=Array(DataType.string))]), ["a"]),  # noqa: UP006
        (
            list[list[str]],
            Schema([ColSpec(type=Array(Array(DataType.string)))]),
            [["a", "b"], ["c"]],
        ),
        (List[List[str]], Schema([ColSpec(type=Array(Array(DataType.string)))]), [["a"], ["b"]]),  # noqa: UP006
        (list[dict[str, str]], Schema([ColSpec(type=Array(Map(DataType.string)))]), [{"a": "b"}]),
        # dictionaries
        (dict[str, int], Schema([ColSpec(type=Map(DataType.long))]), {"a": 1}),
        (Dict[str, int], Schema([ColSpec(type=Map(DataType.long))]), {"a": 1, "b": 2}),  # noqa: UP006
        (dict[str, list[str]], Schema([ColSpec(type=Map(Array(DataType.string)))]), {"a": ["b"]}),
        (
            Dict[str, List[str]],  # noqa: UP006
            Schema([ColSpec(type=Map(Array(DataType.string)))]),
            {"a": ["a", "b"]},
        ),
        # Union
        (Union[int, str], Schema([ColSpec(type=AnyType())]), [1, "a", 234]),
        # Any
        (list[Any], Schema([ColSpec(type=Array(AnyType()))]), [True, "abc", 123]),
        # Pydantic Models
        (
            CustomExample,
            Schema(
                [
                    ColSpec(
                        type=Object(
                            [
                                Property(name="long_field", dtype=DataType.long),
                                Property(name="str_field", dtype=DataType.string),
                                Property(name="bool_field", dtype=DataType.boolean),
                                Property(name="double_field", dtype=DataType.double),
                                Property(name="binary_field", dtype=DataType.binary),
                                Property(name="datetime_field", dtype=DataType.datetime),
                                Property(name="any_field", dtype=AnyType()),
                                Property(
                                    name="optional_str", dtype=DataType.string, required=False
                                ),
                            ]
                        )
                    ),
                ]
            ),
            {
                "long_field": 123,
                "str_field": "abc",
                "bool_field": True,
                "double_field": 1.23,
                "binary_field": b"bytes",
                "datetime_field": datetime.datetime.now(),
                "any_field": ["any", 123],
                "optional_str": "optional",
            },
        ),
        (
            CustomExample2,
            Schema(
                [
                    ColSpec(
                        type=Object(
                            [
                                Property(name="custom_field", dtype=Map(AnyType())),
                                Property(
                                    name="messages",
                                    dtype=Array(
                                        Object(
                                            [
                                                Property(name="role", dtype=DataType.string),
                                                Property(name="content", dtype=DataType.string),
                                            ]
                                        )
                                    ),
                                ),
                                Property(name="optional_int", dtype=DataType.long, required=False),
                            ]
                        )
                    )
                ]
            ),
            {
                "custom_field": {"a": 1},
                "messages": [{"role": "admin", "content": "hello"}],
                "optional_int": 123,
            },
        ),
    ],
)
@pytest.mark.parametrize(
    ("model_type", "has_input_example"),
    # if python_model is callable, input_example should be provided
    [
        ("callable", True),
        ("python_model", True),
        ("python_model", False),
        ("python_model_no_context", True),
        ("python_model_no_context", False),
    ],
)
def test_pyfunc_model_infer_signature_from_type_hints(
    type_hint, expected_schema, input_example, has_input_example, model_type
):
    kwargs = {}
    if model_type == "callable":

        def predict(model_input: type_hint) -> type_hint:
            return model_input

        kwargs["python_model"] = predict
    elif model_type == "python_model":

        class TestModel(mlflow.pyfunc.PythonModel):
            def predict(self, context, model_input: type_hint, params=None) -> type_hint:
                return model_input

        kwargs["python_model"] = TestModel()
    elif model_type == "python_model_no_context":

        class TestModel(mlflow.pyfunc.PythonModel):
            def predict(self, model_input: type_hint, params=None) -> type_hint:
                return model_input

        kwargs["python_model"] = TestModel()

    if has_input_example:
        kwargs["input_example"] = input_example
    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model("test_model", **kwargs)
    assert model_info.signature._is_signature_from_type_hint is True
    assert model_info.signature.inputs == expected_schema
    pyfunc_model = mlflow.pyfunc.load_model(model_info.model_uri)
    if _is_pydantic_type_hint(type_hint):
        if PYDANTIC_V1_OR_OLDER:
            assert pyfunc_model.predict(input_example).dict() == input_example
        else:
            assert pyfunc_model.predict(input_example).model_dump() == input_example
    else:
        assert pyfunc_model.predict(input_example) == input_example


def test_pyfunc_model_with_no_op_type_hint_pass_signature_works():
    def predict(model_input: pd.DataFrame) -> pd.DataFrame:
        return model_input

    input_example = pd.DataFrame({"a": [1]})
    signature = infer_signature(input_example, predict(input_example))
    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "test_model",
            python_model=predict,
            input_example=input_example,
            signature=signature,
        )
    assert model_info.signature.inputs == Schema([ColSpec(type=DataType.long, name="a")])
    pyfunc = mlflow.pyfunc.load_model(model_info.model_uri)
    pd.testing.assert_frame_equal(pyfunc.predict(input_example), input_example)

    class Model(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
            return model_input

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "test_model",
            python_model=Model(),
            input_example=input_example,
        )
    assert model_info.signature.inputs == Schema([ColSpec(type=DataType.long, name="a")])
    pyfunc = mlflow.pyfunc.load_model(model_info.model_uri)
    pd.testing.assert_frame_equal(pyfunc.predict(input_example), input_example)


def test_pyfunc_model_infer_signature_from_type_hints_errors():
    def predict(model_input: int) -> int:
        return model_input

    with mlflow.start_run():
        with mock.patch("mlflow.models.signature._logger.warning") as mock_warning:
            mlflow.pyfunc.log_model("test_model", python_model=predict, input_example="string")
        assert (
            "Input example is not compatible with the type hint of the `predict` function."
            in mock_warning.call_args[0][0]
        )

    def predict(model_input: int) -> str:
        return model_input

    output_hints = _extract_type_hints(predict, 0).output
    with mlflow.start_run():
        with mock.patch("mlflow.models.signature._logger.warning") as mock_warning:
            model_info = mlflow.pyfunc.log_model(
                "test_model", python_model=predict, input_example=123
            )
        assert (
            f"Failed to validate output `123` against type hint `{output_hints}`"
            in mock_warning.call_args[0][0]
        )
        assert model_info.signature.inputs == Schema([ColSpec(type=DataType.long)])
        assert model_info.signature.outputs == Schema([ColSpec(AnyType())])

    class Model(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
            return model_input

    with mlflow.start_run():
        with mock.patch("mlflow.pyfunc._logger.warning") as mock_warning:
            mlflow.pyfunc.log_model("test_model", python_model=Model())
        assert "cannot be used to infer model signature." in mock_warning.call_args[0][0]
        assert (
            "Input example is not provided, model signature cannot be inferred."
            in mock_warning.call_args[0][0]
        )

    with mlflow.start_run():
        with mock.patch("mlflow.pyfunc._logger.warning") as mock_warning:
            mlflow.pyfunc.log_model(
                "test_model", python_model=Model(), input_example=pd.DataFrame()
            )
        assert "cannot be used to infer model signature." in mock_warning.call_args[0][0]
        assert (
            "Inferring model signature from input example failure" in mock_warning.call_args[0][0]
        )


@pytest.mark.skipif(sys.version_info < (3, 10), reason="Requires Python 3.10 or higher")
def test_pyfunc_model_infer_signature_from_type_hints_for_python_3_10():
    def predict(model_input: int | str) -> int | str:
        return model_input

    with mlflow.start_run():
        model_info1 = mlflow.pyfunc.log_model("test_model", python_model=predict, input_example=123)
        model_info2 = mlflow.pyfunc.log_model(
            "test_model", python_model=predict, input_example="string"
        )

    assert model_info1.signature.inputs == Schema([ColSpec(type=AnyType())])
    assert model_info2.signature.outputs == Schema([ColSpec(type=AnyType())])
    assert model_info1.signature == model_info2.signature
    assert model_info1.signature._is_signature_from_type_hint is True
    assert model_info2.signature._is_signature_from_type_hint is True


def save_model_file_for_code_based_logging(type_hint, tmp_path, model_type, extra_def=""):
    if model_type == "callable":
        model_def = f"""
def predict(model_input: {type_hint}) -> {type_hint}:
    return model_input

set_model(predict)
"""
    elif model_type == "python_model":
        model_def = f"""
class TestModel(mlflow.pyfunc.PythonModel):
    def predict(self, context, model_input: {type_hint}, params=None) -> {type_hint}:
        return model_input

set_model(TestModel())
"""
    file_content = f"""
import mlflow
from mlflow.models import set_model

import datetime
import pydantic
from typing import Any, Optional, Union

{extra_def}
{model_def}
"""
    model_path = tmp_path / "model.py"
    model_path.write_text(file_content)
    return {"python_model": model_path}


class TypeHintExample(NamedTuple):
    type_hint: str
    input_example: Any
    extra_def: str = ""


@pytest.mark.parametrize(
    "type_hint_example",
    [
        TypeHintExample("int", 123),
        TypeHintExample("str", "string"),
        TypeHintExample("bool", True),
        TypeHintExample("float", 1.23),
        TypeHintExample("bytes", b"bytes"),
        TypeHintExample("datetime.datetime", datetime.datetime.now()),
        TypeHintExample("Any", "any"),
        TypeHintExample("list[str]", ["a", "b"]),
        TypeHintExample("dict[str, int]", {"a": 1}),
        TypeHintExample("Union[int, str]", 123),
        TypeHintExample(
            "CustomExample2",
            CustomExample2(
                custom_field={"a": 1},
                messages=[Message(role="admin", content="hello")],
                optional_int=123,
            ),
            """
class Message(pydantic.BaseModel):
    role: str
    content: str


class CustomExample2(pydantic.BaseModel):
    custom_field: dict[str, Any]
    messages: list[Message]
    optional_int: Optional[int] = None
""",
        ),
    ],
)
@pytest.mark.parametrize(
    ("model_type", "has_input_example"),
    # if python_model is callable, input_example should be provided
    [("callable", True), ("python_model", True), ("python_model", False)],
)
def test_pyfunc_model_with_type_hints_code_based_logging(
    tmp_path, type_hint_example, model_type, has_input_example
):
    kwargs = save_model_file_for_code_based_logging(
        type_hint_example.type_hint,
        tmp_path,
        model_type,
        type_hint_example.extra_def,
    )
    input_example = type_hint_example.input_example
    if has_input_example:
        kwargs["input_example"] = input_example

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model("test_model", **kwargs)

    assert model_info.signature is not None
    assert model_info.signature._is_signature_from_type_hint is True
    pyfunc_model = mlflow.pyfunc.load_model(model_info.model_uri)
    assert pyfunc_model.predict(input_example) == input_example


def test_functional_python_model_only_input_type_hints():
    def python_model(x: list[str]):
        return x

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "model", python_model=python_model, input_example=["a"]
        )
    assert model_info.signature.inputs == Schema([ColSpec(type=Array(DataType.string))])
    assert model_info.signature.outputs == Schema([ColSpec(AnyType())])


def test_functional_python_model_only_output_type_hints():
    def python_model(x) -> list[str]:
        return x

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "model", python_model=python_model, input_example=["a"]
        )
    assert model_info.signature is None


class CallableObject:
    def __call__(self, x: list[str]) -> list[str]:
        return x


def test_functional_python_model_callable_object():
    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "model", python_model=CallableObject(), input_example=["a"]
        )
    assert model_info.signature.inputs == Schema([ColSpec(type=Array(DataType.string))])
    assert model_info.signature.outputs == Schema([ColSpec(type=Array(DataType.string))])
    loaded_model = mlflow.pyfunc.load_model(model_info.model_uri)
    assert loaded_model.predict(["a", "b"]) == ["a", "b"]


def test_python_model_local_testing():
    class ModelWOTypeHint(mlflow.pyfunc.PythonModel):
        def predict(self, model_input, params=None) -> list[str]:
            return model_input

    class ModelWithTypeHint(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: dict[str, str], params=None) -> list[str]:
            return [model_input["x"]]

    model1 = ModelWOTypeHint()
    assert model1.predict("a") == "a"
    model2 = ModelWithTypeHint()
    assert model2.predict({"x": "a"}) == ["a"]
    with pytest.raises(MlflowException, match=r"Expected dict, but got str"):
        model2.predict("a")


def test_python_model_with_optional_input_local_testing():
    class Model(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: list[Optional[str]], params=None) -> Optional[list[str]]:
            return [x if x is not None else "default" for x in model_input]

    model = Model()
    assert model.predict([None]) == ["default"]
    assert model.predict(["a"]) == ["a"]
    with pytest.raises(MlflowException, match=r"Expected list, but got str"):
        model.predict("a")


def test_callable_local_testing():
    @pyfunc
    def predict(model_input: list[str]) -> list[str]:
        return model_input

    assert predict(["a"]) == ["a"]
    with pytest.raises(MlflowException, match=r"Expected list, but got str"):
        predict("a")

    @pyfunc
    def predict(messages: list[Message]) -> dict[str, str]:
        return {m.role: m.content for m in messages}

    assert predict([Message(role="admin", content="hello")]) == {"admin": "hello"}
    assert predict(
        [{"role": "admin", "content": "hello"}, {"role": "user", "content": "hello"}]
    ) == {"admin": "hello", "user": "hello"}
    pdf = pd.DataFrame([[{"role": "admin", "content": "hello"}]])
    assert predict(pdf) == {"admin": "hello"}

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "model", python_model=predict, input_example=[{"role": "admin", "content": "hello"}]
        )
    pyfunc_model = mlflow.pyfunc.load_model(model_info.model_uri)
    assert pyfunc_model.predict([Message(role="admin", content="hello")]) == {"admin": "hello"}
    assert pyfunc_model.predict(
        [{"role": "admin", "content": "hello"}, {"role": "user", "content": "hello"}]
    ) == {"admin": "hello", "user": "hello"}
    assert pyfunc_model.predict(pdf) == {"admin": "hello"}

    # without decorator
    def predict(messages: list[Message]) -> dict[str, str]:
        return {m.role: m.content for m in messages}

    with pytest.raises(AttributeError, match=r"'dict' object has no attribute 'role'"):
        predict([{"role": "admin", "content": "hello"}])

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "model", python_model=predict, input_example=[{"role": "admin", "content": "hello"}]
        )
    pyfunc_model = mlflow.pyfunc.load_model(model_info.model_uri)
    assert pyfunc_model.predict([{"role": "admin", "content": "hello"}]) == {"admin": "hello"}


def test_no_warning_for_unsupported_type_hint_with_decorator(recwarn):
    warn_msg = "Type hint used in the model's predict function is not supported"

    @pyfunc
    def predict(model_input: pd.DataFrame) -> pd.DataFrame:
        return model_input

    data = pd.DataFrame({"a": [1]})
    predict(data)
    assert not any(warn_msg in str(w.message) for w in recwarn)

    with mlflow.start_run():
        mlflow.pyfunc.log_model("model", python_model=predict, input_example=data)
    assert not any(warn_msg in str(w.message) for w in recwarn)

    class Model(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
            return model_input

    model = Model()
    model.predict(data)
    assert not any(warn_msg in str(w.message) for w in recwarn)

    with mlflow.start_run():
        mlflow.pyfunc.log_model("model", python_model=model, input_example=data)
    assert not any(warn_msg in str(w.message) for w in recwarn)


def test_python_model_local_testing_data_validation():
    class Model(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: list[Message], params=None) -> dict[str, str]:
            return {m.role: m.content for m in model_input}

    model = Model()
    assert model.predict([Message(role="admin", content="hello")]) == {"admin": "hello"}
    assert model.predict(
        [{"role": "admin", "content": "hello"}, {"role": "user", "content": "hello"}]
    ) == {"admin": "hello", "user": "hello"}
    pdf = pd.DataFrame([[{"role": "admin", "content": "hello"}]])
    assert model.predict(pdf) == {"admin": "hello"}

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model(
            "model", python_model=model, input_example=[{"role": "admin", "content": "hello"}]
        )
    pyfunc_model = mlflow.pyfunc.load_model(model_info.model_uri)
    assert pyfunc_model.predict([Message(role="admin", content="hello")]) == {"admin": "hello"}
    assert pyfunc_model.predict(
        [{"role": "admin", "content": "hello"}, {"role": "user", "content": "hello"}]
    ) == {"admin": "hello", "user": "hello"}
    assert pyfunc_model.predict(pdf) == {"admin": "hello"}


def test_python_model_local_testing_same_as_pyfunc_predict():
    class MyModel(mlflow.pyfunc.PythonModel):
        def predict(self, context, model_input: list[str], params=None) -> list[str]:
            return model_input

    model = MyModel()
    with pytest.raises(MlflowException, match=r"Expected list, but got str") as e_local:
        model.predict(None, "a")

    with mlflow.start_run():
        model_info = mlflow.pyfunc.log_model("model", python_model=model, input_example=["a"])
    pyfunc_model = mlflow.pyfunc.load_model(model_info.model_uri)
    with pytest.raises(MlflowException, match=r"Expected list, but got str") as e_pyfunc:
        pyfunc_model.predict("a")

    assert e_local.value.message == e_pyfunc.value.message


def test_invalid_type_hint_in_python_model(recwarn):
    invalid_type_hint_msg = "Type hint used in the model's predict function is not supported"

    class MyModel(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: list[object], params=None) -> str:
            if isinstance(model_input, list):
                return model_input[0]
            return "abc"

    assert any(invalid_type_hint_msg in str(w.message) for w in recwarn)
    recwarn.clear()

    model = MyModel()
    assert model.predict(["a"]) == "a"
    assert not any(invalid_type_hint_msg in str(w.message) for w in recwarn)

    with mlflow.start_run():
        mlflow.pyfunc.log_model("model", python_model=MyModel())
    assert any("Unsupported type hint" in str(w.message) for w in recwarn)


def test_invalid_type_hint_in_callable(recwarn):
    @pyfunc
    def predict(model_input: list[object]) -> str:
        if isinstance(model_input, list):
            return model_input[0]
        return "abc"

    invalid_type_hint_msg = "Type hint used in the model's predict function is not supported"
    assert any(invalid_type_hint_msg in str(w.message) for w in recwarn)
    recwarn.clear()
    # The warning should not be raised again when the function is called
    assert predict(["a"]) == "a"
    assert not any(invalid_type_hint_msg in str(w.message) for w in recwarn)

    with mlflow.start_run():
        mlflow.pyfunc.log_model("model", python_model=predict, input_example=["a"])
    assert any("Unsupported type hint" in str(w.message) for w in recwarn)
    recwarn.clear()

    # without decorator
    def predict(model_input: list[object]) -> str:
        if isinstance(model_input, list):
            return model_input[0]
        return "abc"

    assert predict(["a"]) == "a"
    assert not any(invalid_type_hint_msg in str(w.message) for w in recwarn)

    with mlflow.start_run():
        with pytest.warns(UserWarning, match=r"Unsupported type hint"):
            mlflow.pyfunc.log_model("model", python_model=predict, input_example=["a"])


def test_log_model_warn_only_if_model_with_valid_type_hint_not_decorated(recwarn):
    def predict(model_input: list[str]) -> list[str]:
        return model_input

    with mlflow.start_run():
        mlflow.pyfunc.log_model("model", python_model=predict, input_example=["a"])
        assert any("Decorate your function" in str(w.message) for w in recwarn)
        recwarn.clear()

    class Model(mlflow.pyfunc.PythonModel):
        def predict(self, model_input: list[str], params=None) -> list[str]:
            return model_input

    def predict_df(model_input: pd.DataFrame) -> pd.DataFrame:
        return model_input

    with mlflow.start_run():
        mlflow.pyfunc.log_model("model", python_model=Model(), input_example=["a"])
    assert not any("Decorate your function" in str(w.message) for w in recwarn)
    recwarn.clear()
    with mlflow.start_run():
        mlflow.pyfunc.log_model(
            "model", python_model=predict_df, input_example=pd.DataFrame({"a": [1]})
        )
    assert not any("Decorate your function" in str(w.message) for w in recwarn)
