from math import prod
from .kernel_name_parser import gemm_name_parser

def name2bpe(name):
    lower_name = name.lower()
    if 'float32' in lower_name or lower_name == 'float':
        return 4
    elif 'float16' in lower_name or lower_name in {'c10::half', 'c10::bfloat16'}:
        return 2
    elif 'float8' in lower_name:
        return 1
    else:
        return None
# 1. GEMM 
class GEMM:
    """
    This is the base class for all GEMM operations. 
    If you want to add a new GEMM operation, you should inherit from this class.
    """
    def __init__(self, event):
        self.event = event
        self.param_details = self.get_param_details(event)
        parsed_details = None
        for kernel_name in event['kernel_names']:
            parsed_details = gemm_name_parser(kernel_name)
            if parsed_details is not None:
                break
        if parsed_details is not None:
            self.param_details['transpose'] = parsed_details['transpose']
        self.M, self.N, self.K = self.param_details['M'], self.param_details['N'], self.param_details['K']
        self.bias = self.param_details['bias']
    
    @staticmethod
    def get_param_details(event):
        # to be implemented in the child class
        raise NotImplementedError

    @staticmethod
    def flops_func(M, N, K, bias):
        flops_matmul = 2 * M * N * K
        flops_bias = M * N if bias else 0
        return flops_matmul + flops_bias
    
    def flops(self):
        return self.flops_func(self.M, self.N, self.K, self.bias)

    @staticmethod
    def bytes_func(M, N, K, bias, bpe_mat1, bpe_mat2, bpe_bias, bpe_output):
        #if any of the bpe is None, we will return None
        if None in {bpe_mat1, bpe_mat2, bpe_bias, bpe_output}:
            return None
        bytes_mat1 = M * K * bpe_mat1
        bytes_mat2 = K * N * bpe_mat2
        bytes_output = M * N * bpe_output
        # to be totally accurate we should use the bias shape from profile info
        # but we just assume bias shape as 1xN
        #TODO: use profile info to get the bias shape
        bytes_bias = (N if bias else 0) * bpe_bias
        return bytes_mat1 + bytes_mat2 + bytes_output + bytes_bias
    def bytes(self, bpe_mat1, bpe_mat2, bpe_bias, bpe_output):
        return self.bytes_func(self.M, self.N, self.K, self.bias, bpe_mat1, bpe_mat2, bpe_bias, bpe_output)
    
    """
    bwd pass for Y = X.matmul(W^T) + B
    X_grad = Y_grad.matmul(W)
    W_grad = Y_grad^T.matmul(X)
    B_grad = Y_grad.sum(dim=0)
    """
    
    def flops_bwd(self):
        flops_input_grad = self.flops_func(M=self.M, N=self.K, K=self.N, bias=False)
        flops_weight_grad = self.flops_func(M=self.N, N=self.K, K=self.M, bias=False)
        flops_bias_grad = self.M * self.N if self.bias else 0
        return flops_input_grad + flops_weight_grad + flops_bias_grad

    def bytes_bwd(self, bytes_per_element):
        bytes_input_grad = self.bytes_func(M=self.M, N=self.K, K=self.N, bias=False, bytes_per_element=bytes_per_element)
        bytes_weight_grad = self.bytes_func(M=self.N, N=self.K, K=self.M, bias=False, bytes_per_element=bytes_per_element)
        bytes_bias_grad = self.M * self.N if self.bias else 0
        return bytes_input_grad + bytes_weight_grad + bytes_bias_grad


class aten_mm(GEMM):
    """
    aten::mm the matrix multiplication primitive in PyTorch
    A.matmul(B)
    """
    @staticmethod
    def get_param_details(event):
        input_dims = event['args']['Input Dims']
        A_shape, B_shape = input_dims[0], input_dims[1]
        M = A_shape[0]
        N = B_shape[1]
        K = A_shape[1]

        dtype_A_B = tuple(event['args']['Input type'][:2])
        stride_A = tuple(event['args']['Input Strides'][0])
        stride_B = tuple(event['args']['Input Strides'][1])

        return {"M": M, "N": N, "K": K, "bias": False,
                "stride_A": stride_A, "stride_B": stride_B,
                "dtype_A_B": dtype_A_B}
    
    def bytes(self):
        dtype_A_B = self.param_details['dtype_A_B']
        if dtype_A_B[0] != dtype_A_B[1]:
            raise ValueError(f"Data types of A and B are different: {dtype_A_B}")
        self.bpe = name2bpe(dtype_A_B[0])
        return super().bytes(bpe_mat1=self.bpe, bpe_mat2=self.bpe, 
                             bpe_bias=self.bpe, # does not matter
                             bpe_output=self.bpe) # out dtype is not always provided. #TODO: use out dtype if provided
    def flops_bwd(self):
        raise NotImplementedError("Backward pass for aten::mm is not defined.")
    def bytes_bwd(self, bytes_per_element):
        raise NotImplementedError("Backward pass for aten::mm is not defined.")


class aten_addmm(GEMM):
    """
    aten::addmm is the A.matmul(B) + C operation in PyTorch
    """
    @staticmethod
    def get_param_details(event):
        input_dims = event['args']['Input Dims']
        C_shape, A_shape, B_shape = input_dims[0], input_dims[1], input_dims[2]
        M = A_shape[0]
        N = B_shape[1]
        K = A_shape[1]

        dtype_A_B = tuple(event['args']['Input type'][1:3])
        stride_A = tuple(event['args']['Input Strides'][1])
        stride_B = tuple(event['args']['Input Strides'][2])

        return {"M": M, "N": N, "K": K, "bias": True,
                "stride_A": stride_A, "stride_B": stride_B,
                "dtype_A_B": dtype_A_B}

    def bytes(self):
        dtype_A_B = self.param_details['dtype_A_B']
        if dtype_A_B[0] != dtype_A_B[1]:
            raise ValueError(f"Data types of A and B are different: {dtype_A_B}")
        self.bpe = name2bpe(dtype_A_B[0])
        # setting bias bpe to be the same as the input matrices is not totally correct
        # TODO: correct later
        # TODO: similar to aten_mm, we need to use the output dtype if provided
        return super().bytes(bpe_mat1=self.bpe, bpe_mat2=self.bpe, 
                             bpe_bias=self.bpe, 
                             bpe_output=self.bpe)
    
    def flops_bwd(self):
        raise NotImplementedError("Backward pass for aten::addmm is not defined.")
    def bytes_bwd(self, bytes_per_element):
        raise NotImplementedError("Backward pass for aten::addmm is not defined.")

class aten_scaled_mm(GEMM):
    """
    aten::scaled_mm is the scale_result(scale_a*A.matmul(scale_b*B) + bias)
    """
    @staticmethod
    def get_param_details(event):
        # ref: https://pytorch.org/cppdocs/api/function_namespaceat_1a2902105d8aed3fa448a0da42f90e2cbf.html
        input_dims = event['args']['Input Dims']
        A_shape, B_shape = input_dims[0], input_dims[1]
        M = A_shape[0]
        N = B_shape[1]
        K = A_shape[1]
        bias = len(input_dims) == 3

        dtype_A_B = tuple(event['args']['Input type'][:2])
        stride_A = tuple(event['args']['Input Strides'][0])
        stride_B = tuple(event['args']['Input Strides'][1])
        return {"M": M, "N": N, "K": K, "bias": bias,
                "stride_A": stride_A, "stride_B": stride_B,
                "dtype_A_B": dtype_A_B}

    def bytes(self):
        dtype_A_B = self.param_details['dtype_A_B']
        if dtype_A_B[0] != dtype_A_B[1]:
            raise ValueError(f"Data types of A and B are different: {dtype_A_B}")
        self.bpe = name2bpe(dtype_A_B[0])
        # assumption:
        # for fp8 the output dtype is fp16
        # for fp16, bf16, fp32 the output dtype is the same as the input dtype
        if self.bpe == 1:
            out_bpe = 2
        elif self.bpe in [2, 4]:
            out_bpe = self.bpe
        else:
            out_bpe = None
        return super().bytes(bpe_mat1=self.bpe, bpe_mat2=self.bpe,
                             bpe_bias=self.bpe, # does not matter
                             bpe_output=out_bpe)

    def flops_bwd(self):
        raise NotImplementedError("Backward pass for aten::addmm is not defined.")
    def bytes_bwd(self, bytes_per_element):
        raise NotImplementedError("Backward pass for aten::addmm is not defined.")


# TODO: maybe deprecate aten linear as it will call aten::mm or aten::addmm
class aten_linear(GEMM):    
    
    @staticmethod
    def get_param_details(event):
        input_dims = event['args']['Input Dims']
        input_shape = input_dims[0]
        weight_shape = input_dims[1]
        bias = bool(input_dims[2])
        K = input_shape[-1]
        N = weight_shape[0]
        # Compute M as the product of all dimensions except the last one
        M = 1
        for dim in input_shape[:-1]:
            M *= dim
        
        # TODO: remove repeated code, this is not cool
        dtype_A_B = tuple(event['args']['Input type'][:2])
        stride_A = tuple(event['args']['Input Strides'][0])
        stride_B = tuple(event['args']['Input Strides'][1])

        return {"M": M, "N": N, "K": K, "bias": bias,
                "stride_A": stride_A, "stride_B": stride_B,
                "dtype_A_B": dtype_A_B}

# 2. Convolution
class CONV:
    # Conv perf model is based on: https://github.com/pytorch/pytorch/blob/main/torch/utils/flop_counter.py
    # we will make stuff reusiable across conv1d, conv2d, and conv3d
    def __init__(self, event):
        self.event = event
        self.param_details = self.get_param_details(event)
        self.x_shape, self.w_shape = self.param_details['input_shape'], self.param_details['filter_shape']
        self.stride, self.padding, self.dilation, self.groups = ( self.param_details[key] for key in ['stride', 'padding', 'dilation', 'groups'])
        self.bias = self.param_details['bias']
        self.transposed_conv = self.param_details['transposed_conv']
        self.out_shape = CONV.get_output_shape(self.x_shape, self.w_shape, self.stride, self.padding, self.dilation, self.transposed_conv)
    
    @staticmethod
    def get_output_shape(input_shape, filter_shape, stride, padding, dilation, transposed_conv):
        x_spatial_shape, w_spatial_shape = input_shape[2:], filter_shape[2:]
        conv_ndims = len(x_spatial_shape)
        spatial_out_fn = CONV.get_conv_out_dim if not transposed_conv else CONV.get_transposed_conv_out_dim
        out_spatial_shape = tuple(spatial_out_fn(x_spatial_shape[i], w_spatial_shape[i], 
                                                stride[i], padding[i], dilation[i]) for i in range(conv_ndims))
        return (input_shape[0], filter_shape[0]) + tuple(out_spatial_shape)
    
    @staticmethod
    def t(shape):
        return (shape[1], shape[0]) + shape[2:]

    @staticmethod
    def get_conv_out_dim(input_dim, kernel_size, stride, padding, dilation):
        return int(((input_dim + 2 * padding - dilation * (kernel_size - 1) - 1) / stride) + 1)

    @staticmethod
    def get_transposed_conv_out_dim(input_dim, kernel_size, stride, padding, dilation, output_padding):
        return (input_dim - 1) * stride - 2 * padding + dilation * (kernel_size - 1) + output_padding + 1

    @staticmethod
    def flops_func(x_shape, w_shape, out_shape, bias, transposed_conv=False):
        # c_in =filter[1] already accounts for grouped convolutions
        flops_per_element = 2 * prod(w_shape[1:])
        if transposed_conv:
            flops_conv = prod(x_shape) * flops_per_element
        else:
            flops_conv = prod(out_shape) * flops_per_element
        flops_bias = prod(out_shape) if bias else 0
        return flops_conv + flops_bias
    def flops(self):
        return self.flops_func(self.x_shape, self.w_shape, self.out_shape,
                                self.bias, self.transposed_conv)

    @staticmethod
    # we assume same bytes per element for all tensors
    # TODO: make it more general later
    def bytes_func(x_shape, w_shape, out_shape, bias, bytes_per_element):
        if bytes_per_element is None:
            return None
        elems_input_read = prod(x_shape)
        elems_weight_read = prod(w_shape)
        elems_bias_read = out_shape[1] if bias else 0
        elems_output_write = prod(out_shape)
        total_elems_moved = elems_input_read + elems_weight_read + elems_bias_read + elems_output_write
        return total_elems_moved * bytes_per_element
    def bytes(self, bytes_per_element):
        return self.bytes_func(self.x_shape, self.w_shape, self.out_shape, self.bias, bytes_per_element)

    @staticmethod
    def flops_bwd_func(out_shape, x_shape, w_shape, bias, transposed_conv=False):
        flops_input_grad = CONV.flops_func(out_shape, w_shape, x_shape, False, not transposed_conv)
        if not transposed_conv:
            flops_weight_grad = CONV.flops_func(CONV.t(x_shape), CONV.t(out_shape), CONV.t(w_shape), False, False)
        else:
            flops_weight_grad = CONV.flops_func(CONV.t(out_shape), CONV.t(x_shape), CONV.t(w_shape), False, False)

        flops_bias_grad = prod(out_shape) if bias else 0
        return flops_input_grad + flops_weight_grad + flops_bias_grad
    def flops_bwd(self):
        return self.flops_bwd_func(self.out_shape, self.x_shape, self.w_shape, self.bias, self.transposed_conv)
    
    @staticmethod
    def bytes_bwd_func(x_shape, w_shape, out_shape, bias, bytes_per_element):
        if bytes_per_element is None:
            return None
        bytes_input_grad = CONV.bytes_func(out_shape, w_shape, x_shape, False, bytes_per_element)
        bytes_weight_grad = CONV.bytes_func(out_shape, x_shape, w_shape, False, bytes_per_element)
        # for bias we read the output gradient and write the bias gradient
        bytes_bias_grad = prod(out_shape) + out_shape[1] if bias else 0
        return bytes_input_grad + bytes_weight_grad + bytes_bias_grad
    def bytes_bwd(self, bytes_per_element):
        return self.bytes_bwd_func(self.x_shape, self.w_shape, self.out_shape, self.bias, bytes_per_element)
    
    @staticmethod
    def get_param_details(event):
        # to be implemented in the child class
        raise NotImplementedError
    
class aten_conv(CONV):

    @staticmethod
    def str_to_tuple(s):
        return tuple(int(x) for x in s[1:-1].split(','))
    @staticmethod
    def get_param_details(event):
        # 0 input tensor
        # 1 weight tensor
        # 2 bias tensor (optional)
        # 3 stride
        # 4 padding
        # 5 dilation
        # 6 transposed (boolean)
        # 7 output_padding
        # 8 groups
        input_dims = event['args']['Input Dims']
        concrete_inputs = event['args']['Concrete Inputs']

        input_shape = tuple(input_dims[0])
        ndims = len(input_shape) - 2 #first two dimensions are batch and channel
        filter_shape = tuple(input_dims[1])
        bias = len(input_dims) == 3

        
        stride_arg = concrete_inputs[3]
        stride = aten_conv.str_to_tuple(stride_arg) if stride_arg != '' else (1,) * ndims
        padding_arg = concrete_inputs[4]
        padding = aten_conv.str_to_tuple(padding_arg) if padding_arg != '' else (0,) * ndims
        dilation_arg = concrete_inputs[5]
        dilation = aten_conv.str_to_tuple(dilation_arg) if dilation_arg != '' else (1,) * ndims
        transposed_conv = eval(concrete_inputs[6])
        output_padding_arg = concrete_inputs[7]
        output_padding = aten_conv.str_to_tuple(output_padding_arg) if output_padding_arg != '' else (0,) * ndims
        groups = int(concrete_inputs[8])

        # if its a length 1 tuple then we broadcast it to the number of spatial dimensions
        stride, padding, dilation, output_padding = [
            param * ndims if len(param) == 1 else param
            for param in [stride, padding, dilation, output_padding]
        ]

        dtype_input_weight = tuple(event['args']['Input type'][:2])
        # check no mixed precision
        if dtype_input_weight[0] != dtype_input_weight[1]:
            raise ValueError(f"Data types of input and weight are different: {dtype_input_weight}")
        input_stride = tuple(event['args']['Input Strides'][0])
        weight_stride = tuple(event['args']['Input Strides'][1])

        return {"input_shape": input_shape, "filter_shape": filter_shape, "dtype_input_weight": dtype_input_weight,
                "input_stide": input_stride, "weight_stride": weight_stride,
                "bias": bias, "stride": stride, "padding": padding, "dilation": dilation,
                "transposed_conv": transposed_conv, "output_padding": output_padding,
                "groups": groups}
    
    def bytes(self):
        dtype_input_weight = self.param_details['dtype_input_weight']
        if dtype_input_weight[0] != dtype_input_weight[1]:
            raise ValueError(f"Data types of input and weight are different: {dtype_input_weight}")
        self.bpe = name2bpe(dtype_input_weight[0])
        return super().bytes(self.bpe)

    def bytes_bwd(self):
        dtype_input_weight = self.param_details['dtype_input_weight']
        if dtype_input_weight[0] != dtype_input_weight[1]:
            raise ValueError(f"Data types of input and weight are different: {dtype_input_weight}")
        self.bpe = name2bpe(dtype_input_weight[0])
        return super().bytes_bwd(self.bpe)


class aten_conv_bwd(aten_conv):
    def __init__(self, event):
        super().__init__(event)
    
    def flops(self):
        return self.flops_bwd()
    
    def bytes(self, bytes_per_element):
        return self.bytes_bwd(bytes_per_element)
class SDPA:

    def __init__(self, event):
        self.event = event
        self.param_details = self.get_param_details(event)
        # get useful stuff from the param_details
        self.B, self.N_Q, self.H, self.d_k, self.N_K = (self.param_details[key] for key in ['B', 'N_Q', 'H', 'd_k', 'N_K'])
    
    def get_param_details(event):
        # to be implemented in the child class
        raise NotImplementedError
    
    @staticmethod
    def flops_func(B, N_Q, H, d_k, N_K, dropout, causal):
        if causal:
            raise ValueError("Not implemented for causal=True")
        if dropout != 0.0:
            raise ValueError(f"Not implemented for dropout={dropout}")
        flops_qk = 2 * B * N_Q * H * d_k * N_K
        # not including softmax for now
        flops_pv = 2 * B * N_Q * H * N_K *d_k
        return flops_qk + flops_pv
    def flops(self):
        return self.flops_func(self.B, self.N_Q, self.H, self.d_k, self.N_K,
                                self.param_details['dropout'], self.param_details['causal'])
    
    @staticmethod
    def bytes_func(B, N_Q, H, d_k, N_K, dropout, causal, bytes_per_element):
        if dropout != 0.0:
            raise ValueError(f"Not implemented for dropout={dropout}")
        if causal:
            raise ValueError("Not implemented for causal=True")
        elems_q_read = B * N_Q * d_k * H
        elems_kv_read = 2 * B * N_K * d_k * H
        elems_out_write = B * N_Q * d_k * H
        total_elems_moved = elems_q_read + elems_kv_read + elems_out_write
        return total_elems_moved * bytes_per_element
    #TODO make bytes_per_element based on profile info
    def bytes(self, bytes_per_element=2):
        return self.bytes_func(self.B, self.N_Q, self.H, self.d_k, self.N_K,
                                self.param_details['dropout'], self.param_details['causal'], bytes_per_element)
    
    @staticmethod
    def flops_bwd_func(B, N_Q, H, d_k, N_K, dropout, causal, flash_impl):
        if causal:
            raise ValueError("Not implemented for causal=True")
        if dropout != 0.0:
            raise ValueError(f"Not implemented for dropout={dropout}")
        flops_recompute_qk = 2 * B * N_Q * H * d_k * N_K if flash_impl else 0

        # not including softmax for now
        flops_v_grad = 2 * B * N_Q * H * d_k * N_K
        flops_s_grad = 2 * B * N_Q * H * d_k * N_K
        flops_q_grad = 2 * B * N_Q * H * d_k * N_K
        flops_k_grad = 2 * B * N_Q * H * d_k * N_K

        return flops_v_grad + flops_s_grad + flops_q_grad + flops_k_grad + flops_recompute_qk
    def flops_bwd(self):
        return self.flops_bwd_func(self.B, self.N_Q, self.H, self.d_k, self.N_K,
                                    self.param_details['dropout'], self.param_details['causal'], self.param_details['flash_impl'])

    # @staticmethod
    # def bytes_bwd_func(B, N_Q, H, d_k, N_K, dropout, causal, flash_impl, bytes_per_element):
    def bytes_bwd(self, bytes_per_element=2):
        # not implemented for now
        return None

class flash_attention(SDPA):
    
    @staticmethod
    def get_param_details(event):
        input_dims = event['args']['Input Dims']
        B, N_Q, H, d_k = input_dims[0]
        _, N_K, _, _ = input_dims[1]
        _, _, _, _ = input_dims[2]
        dropout = float(event['args']['Concrete Inputs'][3])
        causal = eval(event['args']['Concrete Inputs'][5])
        return {"B": B, "N_Q": N_Q, "N_K": N_K, "H": H, "d_k": d_k,
                "dropout": dropout, "causal": causal, "flash_impl": True}

class flash_attention_backward(flash_attention):
    
    def __init__(self, event):
        super().__init__(event)
    
    def flops(self):
        return self.flops_bwd()
    
    def bytes(self, bytes_per_element):
        return self.bytes_bwd(bytes_per_element)
