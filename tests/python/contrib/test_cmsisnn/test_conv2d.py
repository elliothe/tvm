# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""CMSIS-NN integration tests: Conv2D"""
import itertools
import numpy as np
import pytest
import tvm
from tvm import relay
from tvm.relay.op.contrib import cmsisnn


from tests.python.relay.aot.aot_test_utils import (
    AOTTestModel,
    AOT_CORSTONE300_RUNNER,
    AOT_DEFAULT_RUNNER,
    generate_ref_data,
    compile_and_run,
)
from utils import (
    skip_if_no_reference_system,
    make_module,
    count_num_calls,
    get_range_for_dtype_str,
    get_same_padding,
    get_conv2d_qnn_params,
    make_qnn_relu,
)


def make_model(
    shape,
    kernel_shape,
    input_zero_point,
    input_scale,
    kernel_zero_point,
    kernel_scale,
    output_zero_point,
    output_scale,
    padding,
    strides,
    dilation,
    groups,
    dtype,
    kernel_dtype,
    out_channels,
    weight_format,
    enable_bias,
    relu_type,
):
    """Return a model and any parameters it may have"""
    h_index = weight_format.index("H")
    w_index = weight_format.index("W")
    kernel_h = kernel_shape[h_index]
    kernel_w = kernel_shape[w_index]
    a = relay.var("input", shape=shape, dtype=dtype)
    p = (0, 0, 0, 0)
    if padding == "SAME":
        p = get_same_padding((shape[1], shape[2]), (kernel_h, kernel_w), dilation, strides)
        a = relay.nn.pad(
            a,
            pad_width=[(0, 0), (p[0], p[2]), (p[1], p[3]), (0, 0)],
            pad_value=input_zero_point,
            pad_mode="constant",
        )
        shape = (shape[0], shape[1] + p[0] + p[2], shape[2] + p[1] + p[3], shape[3])

    weight_shape = (kernel_h, kernel_w, shape[3] // groups, out_channels)
    rng = np.random.default_rng(12321)
    w = tvm.nd.array(
        rng.integers(
            np.iinfo(kernel_dtype).min,
            high=np.iinfo(kernel_dtype).max,
            size=weight_shape,
            dtype=kernel_dtype,
        )
    )
    weight_const = relay.const(w, kernel_dtype)
    conv = relay.qnn.op.conv2d(
        a,
        weight_const,
        input_zero_point=relay.const(input_zero_point, "int32"),
        kernel_zero_point=relay.const(kernel_zero_point, "int32"),
        input_scale=relay.const(input_scale, "float32"),
        kernel_scale=relay.const(kernel_scale, "float32"),
        kernel_size=(kernel_h, kernel_w),
        data_layout="NHWC",
        kernel_layout=weight_format,
        dilation=dilation,
        strides=strides,
        groups=groups,
        channels=out_channels,
        padding=p,
        out_dtype="int32",
    )
    b = tvm.nd.array(rng.integers(0, high=10, size=(out_channels,), dtype="int32"))
    bias_const = relay.const(b, "int32")
    last_op = relay.nn.bias_add(conv, bias_const, axis=3) if enable_bias else conv
    requant_input_sc = [sc * input_scale for sc in kernel_scale]
    last_op = relay.qnn.op.requantize(
        last_op,
        relay.const(requant_input_sc, "float32"),
        relay.const(0, "int32"),
        relay.const(output_scale, "float32"),
        relay.const(output_zero_point, "int32"),
        out_dtype=dtype,
    )
    last_op = make_qnn_relu(last_op, relu_type, output_scale, output_zero_point, dtype)
    params = {"w": w, "b": b}
    return last_op, params


@tvm.testing.requires_cmsisnn
@pytest.mark.parametrize("ifm_shape", [(1, 28, 28, 12), (1, 64, 100, 4)])
@pytest.mark.parametrize("kernel_size", [(3, 3)])
@pytest.mark.parametrize("padding", ["SAME", "VALID"])
@pytest.mark.parametrize("strides, dilation", [((2, 2), (1, 1)), ((1, 1), (1, 1))])
@pytest.mark.parametrize("enable_bias", [True, False])
@pytest.mark.parametrize("relu_type", ["NONE", "RELU"])
@pytest.mark.parametrize(
    "input_zero_point, input_scale, kernel_scale, out_channels",
    [(10, 0.0128, [0.11, 0.22], 2), (-64, 1, [1, 0.0256, 1.37], 3)],
)
def test_op_int8(
    ifm_shape,
    kernel_size,
    padding,
    strides,
    dilation,
    enable_bias,
    relu_type,
    input_zero_point,
    input_scale,
    kernel_scale,
    out_channels,
):
    interface_api = "c"
    use_unpacked_api = True
    test_runner = AOT_CORSTONE300_RUNNER

    kernel_zero_point = 0
    groups = 1
    weight_format = "HWIO"
    kernel_h = kernel_size[0]
    kernel_w = kernel_size[1]
    dtype = "int8"
    in_min, in_max = get_range_for_dtype_str(dtype)

    weight_shape = None
    if weight_format == "HWIO":
        weight_shape = (kernel_h, kernel_w, ifm_shape[3] // groups, out_channels)
    else:
        weight_shape = (kernel_h, kernel_w, ifm_shape[3], out_channels)

    output_scale, output_zero_point = get_conv2d_qnn_params(
        weight_shape,
        input_scale,
        input_zero_point,
        kernel_scale,
        kernel_zero_point,
        dtype,
        dtype,
        dtype,
        False,
    )

    model, params = make_model(
        ifm_shape,
        weight_shape,
        input_zero_point,
        input_scale,
        kernel_zero_point,
        kernel_scale,
        output_zero_point,
        output_scale,
        padding,
        strides,
        dilation,
        groups,
        dtype,
        dtype,
        out_channels,
        weight_format,
        enable_bias,
        relu_type,
    )
    orig_mod = make_module(model)
    cmsisnn_mod = cmsisnn.partition_for_cmsisnn(orig_mod, params)

    # validate pattern matching
    attrs = [
        cmsisnn_mod[var.name_hint].attrs
        for var in cmsisnn_mod.get_global_vars()
        if cmsisnn_mod[var.name_hint].attrs
    ]
    assert any(attrs), "At least one function with external attributes was expected."

    compilers = [
        key == "Compiler" and value == "cmsis-nn" for attr in attrs for key, value in attr.items()
    ]
    assert any(compilers), "Module does not contain function for cmsis-nn target."

    assert count_num_calls(orig_mod) == count_num_calls(
        cmsisnn_mod
    ), "Number of calls changed during partitioning"

    # validate the output
    rng = np.random.default_rng(12345)
    inputs = {"input": rng.integers(in_min, high=in_max, size=ifm_shape, dtype=dtype)}
    output_list = generate_ref_data(orig_mod["main"], inputs, params)
    compile_and_run(
        AOTTestModel(
            module=cmsisnn_mod,
            inputs=inputs,
            outputs=output_list,
            params=params,
            output_tolerance=1,
        ),
        test_runner,
        interface_api,
        use_unpacked_api,
    )


def parameterize_for_invalid_model(test):
    in_dtype = ["uint8", "int8"]
    kernel_dtype = ["uint8", "int8"]
    kernel_zero_point = [-33, 10, 0]
    all_combinations = itertools.product(in_dtype, kernel_dtype, kernel_zero_point)
    all_combinations = filter(
        lambda parameters: not (
            parameters[0] == "int8" and parameters[1] == "int8" and parameters[2] == 0
        ),
        all_combinations,
    )
    return pytest.mark.parametrize(
        ["in_dtype", "kernel_dtype", "kernel_zero_point"],
        all_combinations,
    )(test)


@tvm.testing.requires_cmsisnn
@parameterize_for_invalid_model
def test_invalid_parameters(
    in_dtype,
    kernel_dtype,
    kernel_zero_point,
):
    ifm_shape = (1, 28, 28, 12)
    out_channels = 2
    input_scale = 1
    input_zero_point = 24
    kernel_scale = [0.11, 0.0237]
    in_min, in_max = get_range_for_dtype_str(in_dtype)

    kernel_layout = "HWIO"
    kernel_shape = [3, 3, ifm_shape[3], out_channels]
    output_scale, output_zero_point = get_conv2d_qnn_params(
        kernel_shape,
        input_scale,
        input_zero_point,
        kernel_scale,
        kernel_zero_point,
        in_dtype,
        kernel_dtype,
        in_dtype,
        False,
    )
    model, params = make_model(
        shape=ifm_shape,
        kernel_shape=kernel_shape,
        input_zero_point=input_zero_point,
        input_scale=input_scale,
        kernel_zero_point=kernel_zero_point,
        kernel_scale=kernel_scale,
        output_zero_point=output_zero_point,
        output_scale=output_scale,
        padding="SAME",
        strides=(1, 1),
        dilation=(1, 1),
        groups=1,
        dtype=in_dtype,
        kernel_dtype=kernel_dtype,
        out_channels=out_channels,
        weight_format=kernel_layout,
        enable_bias=True,
        relu_type="NONE",
    )
    orig_mod = make_module(model)
    cmsisnn_mod = cmsisnn.partition_for_cmsisnn(orig_mod, params)

    # validate pattern matching
    attrs = [
        cmsisnn_mod[var.name_hint].attrs
        for var in cmsisnn_mod.get_global_vars()
        if cmsisnn_mod[var.name_hint].attrs
    ]
    assert not any(attrs), "No function should have an external attribute."


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
