import math
import inspect
import torch
import torch.nn as nn
from torch.nn import functional as F
from transformers import GPT2LMHeadModel
from ..config import Config as cfg

class GPT(nn.Module):
    """
    A class that represents the GPT model.

    ...

    Attributes
    ----------
    transformer : nn.ModuleDict
        Contains the transformer blocks and layers of the GPT model.
    lm_head : nn.Linear
        The linear layer at the end of the GPT model.
        
    Methods
    -------
    get_num_params(non_embedding=True):
        Returns the number of parameters in the model.
        
    _init_weights(module):
        Initializes weights of the given module based on its type.
        
    forward(idx, targets=None):
        Performs the forward pass of the GPT model.
        
    crop_block_size(block_size):
        Modifies the block size of the model if necessary.
        
    from_pretrained(model_type, override_args=None):
        Loads the model with the weights of a pretrained model of the specified type.
        
    configure_optimizers(weight_decay, learning_rate, betas, device_type):
        Configures and returns the AdamW optimizer for the model.
        
    estimate_mfu(fwdbwd_per_iter, dt):
        Estimates the model flops utilization (MFU) in units of A100 bfloat16 peak FLOPS.
        
    generate(idx, max_new_tokens, temperature=1.0, top_k=None):
        Completes the given sequence with new tokens generated by the model.
    """

    def __init__(self):
        super().__init__()
        assert cfg.gpt.vocab_size is not None
        assert cfg.gpt.block_size is not None

        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(cfg.gpt.vocab_size, cfg.gpt.n_embd),
            wpe = nn.Embedding(cfg.gpt.block_size, cfg.gpt.n_embd),
            drop = nn.Dropout(cfg.gpt.dropout),
            h = nn.ModuleList([Block(cfg.gpt) for _ in range(cfg.gpt.n_layer)]),
            ln_f = LayerNorm(),
        ))
        self.lm_head = nn.Linear(cfg.gpt.n_embd, cfg.gpt.vocab_size, bias=False)
        # with weight tying when using torch.compile() some warnings get generated:
        # "UserWarning: functional_call was passed multiple values for tied weights.
        # This behavior is deprecated and will be an error in future versions"
        # not 100% sure what this is, so far seems to be harmless. TODO investigate
        self.transformer.wte.weight = self.lm_head.weight # https://paperswithcode.com/method/weight-tying

        # init all weights
        self.apply(self._init_weights)
        # apply special scaled init to the residual projections, per GPT-2 paper
        for pn, p in self.named_parameters():
            if pn.endswith('c_proj.weight'):
                torch.nn.init.normal_(p, mean=0.0, std=0.02/math.sqrt(2 * cfg.gpt.n_layer))

        # report number of parameters
        print("number of parameters: %.2fM" % (self.get_num_params()/1e6,))

    def get_num_params(self, non_embedding=True):
        """
        Returns the total number of parameters in the GPT model. By default, the method subtracts the position embeddings
        count from the total number. Token embeddings are included in the count since they are used as weights in the final layer
        due to parameter sharing.

        This method is especially useful in evaluating model complexity and determining memory requirements.

        Args:
            non_embedding (bool, optional): Determines whether the count of position embeddings should be subtracted from the total.
                The default value is True, implying the count of position embeddings will be subtracted.

        Returns:
            int: The total number of parameters in the model. If `non_embedding` is True, the number excludes position embedding parameters.

        Note:
            Position embeddings are subtracted because they are not involved in the actual computations of the transformer and 
            hence do not contribute to the model's complexity. However, token embeddings are included since they are shared with 
            the final linear layer and actively contribute to the model's predictions.
        """

        n_params = sum(p.numel() for p in self.parameters())
        if non_embedding:
            n_params -= self.transformer.wpe.weight.numel()
        return n_params

    def _init_weights(self, module):
        """
        Initializes the weights of the given PyTorch module. 

        If the module is a linear layer (nn.Linear), it initializes the weights to a normal distribution with mean=0 and std_dev=0.02.
        If the linear layer has a bias term, the method initializes it to zeros. 

        If the module is an embedding layer (nn.Embedding), it initializes the weights to a normal distribution with mean=0 and std_dev=0.02.

        This method is applied recursively to every submodule in the model when the model is created.

        Args:
            module (torch.nn.Module): A module from the PyTorch model for which the weights are to be initialized.

        Returns:
            None

        Note:
            The initializations for both nn.Linear and nn.Embedding layers are standard practices in NLP tasks to help the model converge faster.
        """
        # Check if the given module is a Linear layer
        if isinstance(module, nn.Linear):
            # If so, initialize the weight matrix of the Linear layer with a normal distribution
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            # If the Linear layer has a bias term, initialize it with zeros
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        # Check if the given module is an Embedding layer
        elif isinstance(module, nn.Embedding):
            # If so, initialize the weight matrix of the Embedding layer with a normal distribution
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        """
        Implements the forward pass for the GPT model.

        The forward method first checks if the length of the sequence to be processed is less than or equal to the block size of the model.
        It then creates the position embeddings and sums them with the token embeddings, applying dropout after.
        The sum of the embeddings is then passed through each transformer block in sequence.
        The output is finally passed through a layer normalization.
        If targets are provided, the method calculates the cross entropy loss, else it outputs the logits for the last position only.

        Args:
            idx (torch.Tensor): A tensor containing the input sequence, 
                                with dimensions [batch_size, sequence_length].
            targets (torch.Tensor, optional): A tensor containing the target sequence, 
                                            with the same dimensions as idx. Defaults to None.

        Returns:
            tuple: A tuple containing:
                - logits (torch.Tensor): The model's predictions for each position in the sequence, 
                                            with dimensions [batch_size, sequence_length, vocab_size].
                - loss (torch.Tensor or None): The cross entropy loss between the logits and targets,
                                                or None if targets were not provided.

        Raises:
            AssertionError: If the length of the sequence to be processed exceeds the model's block size.

        Examples:
            >>> gpt_model = GPT()  # assuming GPT is already initialized
            >>> input_ids = torch.randint(0, gpt_model.cfg.gpt.vocab_size, (1, gpt_model.cfg.gpt.block_size))  # random sequence
            >>> output_logits, output_loss = gpt_model.forward(input_ids)

        """
        device = idx.device
        b, t = idx.size()
        assert t <= self.cfg.gpt.block_size, f"Cannot forward sequence of length {t}, block size is only {self.cfg.gpt.block_size}"
        pos = torch.arange(0, t, dtype=torch.long, device=device) # shape (t)

        # forward the GPT model itself
        tok_emb = self.transformer.wte(idx) # token embeddings of shape (b, t, n_embd)
        pos_emb = self.transformer.wpe(pos) # position embeddings of shape (t, n_embd)
        x = self.transformer.drop(tok_emb + pos_emb)
        for block in self.transformer.h:
            x = block(x)
        x = self.transformer.ln_f(x)

        if targets is not None:
            # if we are given some desired targets also calculate the loss
            logits = self.lm_head(x)
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
        else:
            # inference-time mini-optimization: only forward the lm_head on the very last position
            logits = self.lm_head(x[:, [-1], :]) # note: using list [-1] to preserve the time dim
            loss = None

        return logits, loss

    def crop_block_size(self, block_size):
        """
        Crops the model's block size to a smaller value if necessary.

        This method performs model surgery to decrease the block size if necessary.
        For example, we may load the GPT-2 pretrained model checkpoint (which has a block size of 1024) 
        but want to use a smaller block size for some smaller, simpler model. 
        The method updates the block size in the model's configuration, 
        crops the weights for the position embeddings, 
        and also crops the attention bias if it exists.

        Args:
            block_size (int): The new block size to crop the model to.

        Raises:
            AssertionError: If the new block size is larger than the current block size.

        Examples:
            >>> gpt_model = GPT()  # assuming GPT is already initialized
            >>> gpt_model.crop_block_size(512)  # reduce the block size to 512

        """
        assert block_size <= self.cfg.gpt.block_size, "New block size must be smaller than or equal to the current block size"

        # Update the block size in the model's configuration
        self.cfg.gpt.block_size = block_size

        # Crop the weights for the position embeddings to the new block size
        self.transformer.wpe.weight = nn.Parameter(self.transformer.wpe.weight[:block_size])

        # Iterate over each transformer block in the model
        for block in self.transformer.h:
            # Check if the transformer block has attention bias
            if hasattr(block.attn, 'bias'):
                # If it does, crop the attention bias to the new block size
                block.attn.bias = block.attn.bias[:,:,:block_size,:block_size]

    @classmethod
    def from_pretrained(cls, model_type, override_args=None):
        """
        Creates a GPT model from a pretrained model.

        This method initializes a GPT model with the architecture and weights of a pretrained GPT model.
        The method supports 'gpt2', 'gpt2-medium', 'gpt2-large', and 'gpt2-xl' as the model_type.
        For these models, the method determines n_layer, n_head and n_embd from the model_type.
        The model's configuration is then updated with these values.
        The dropout rate can be overridden if desired.
        Finally, the method aligns and matches the pretrained model's state_dict with the new model's state_dict,
        and copies the pretrained model's weights into the new model's weights.

        Args:
            model_type (str): The type of the pretrained model. Can be 'gpt2', 'gpt2-medium', 'gpt2-large', or 'gpt2-xl'.
            override_args (dict, optional): A dictionary of model configurations to override. Defaults to None.

        Returns:
            GPT: The GPT model initialized with the pretrained model's weights.

        Raises:
            AssertionError: If the model_type is not supported, or if the state_dicts of the pretrained model and the new model are not compatible.

        Examples:
            >>> GPT.from_pretrained('gpt2')  # initializes a GPT model with the architecture and weights of the 'gpt2' pretrained model
            >>> GPT.from_pretrained('gpt2', {'dropout': 0.1})  # initializes a GPT model with the architecture and weights of the 'gpt2' pretrained model, and sets the dropout rate to 0.1

        """
        assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}, "Model type must be one of 'gpt2', 'gpt2-medium', 'gpt2-large', or 'gpt2-xl'"
        override_args = override_args or {}  # default to empty dict if no override_args were provided

        # Only dropout can be overridden
        assert all(k == 'dropout' for k in override_args), "Only 'dropout' can be overridden"
        
        # Determine n_layer, n_head and n_embd from model_type
        cfg.gpt_args = {
            'gpt2':         dict(n_layer=12, n_head=12, n_embd=768),  # 124M params
            'gpt2-medium':  dict(n_layer=24, n_head=16, n_embd=1024), # 350M params
            'gpt2-large':   dict(n_layer=36, n_head=20, n_embd=1280), # 774M params
            'gpt2-xl':      dict(n_layer=48, n_head=25, n_embd=1600), # 1558M params
        }[model_type]

        # Force vocab_size=50257, block_size=1024, and bias=True
        cfg.gpt_args['vocab_size'] = 50257  # always 50257 for GPT model checkpoints
        cfg.gpt_args['block_size'] = 1024  # always 1024 for GPT model checkpoints
        cfg.gpt_args['bias'] = True  # always True for GPT model checkpoints

        # Override the dropout rate, if desired
        if 'dropout' in override_args:
            cfg.gpt_args['dropout'] = override_args['dropout']

        # Initialize a GPT model
        model = GPT()
        
        # Obtain the state_dict of the new model and the keys in the state_dict
        sd = model.state_dict()
        sd_keys = list(sd.keys())

        # Ignore the '.attn.bias' parameter in the state_dict of the new model
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')]

        # Initialize a pretrained GPT model from Hugging Face's Transformers library
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)

        # Obtain the state_dict of the pretrained model and the keys in the state_dict
        sd_hf = model_hf.state_dict()
        sd_keys_hf = list(sd_hf.keys())

        # Ignore the '.attn.masked_bias' and '.attn.bias' parameters in the state_dict of the pretrained model
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias') and not k.endswith('.attn.bias')]

        # Ensure all of the parameters are aligned and match in names and shapes
        assert len(sd_keys_hf) == len(sd_keys), "Mismatched keys in the state_dicts of the new model and the pretrained model"

        # Copy the weights from the pretrained model's state_dict to the new model's state_dict
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']):
                # Special treatment for the Conv1D weights which need to be transposed
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                # Vanilla copy for the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])

        return model

    def configure_optimizers(self, weight_decay, learning_rate, betas, device_type):
        """
        Configures the optimizer for the GPT model.

        This method first collects all the model parameters that require gradients.
        It then groups these parameters into two groups based on their dimensionality.
        Any parameters that are 2D will have weight decay applied to them; all others will not.
        The method then creates an AdamW optimizer with the given learning rate, betas, and weight decay settings.
        The method uses the fused version of AdamW if it is available and if the device type is CUDA.

        Args:
            weight_decay (float): The weight decay (L2 penalty) to apply to the parameters.
            learning_rate (float): The learning rate for the optimizer.
            betas (tuple): The coefficients used for computing running averages of gradient and its square.
            device_type (str): The type of device to run the model on. Can be 'cpu' or 'cuda'.

        Returns:
            torch.optim.AdamW: The configured AdamW optimizer.

        Examples:
            >>> gpt = GPT()
            >>> optimizer = gpt.configure_optimizers(0.01, 0.001, (0.9, 0.999), 'cuda')
        """
        # Get all parameters of the model that require gradients
        param_dict = {pn: p for pn, p in self.named_parameters() if p.requires_grad}

        # Group the parameters based on their dimensionality
        decay_params = [p for p in param_dict.values() if p.dim() >= 2]  # 2D parameters will have weight decay
        nodecay_params = [p for p in param_dict.values() if p.dim() < 2]  # non-2D parameters will not have weight decay

        # Define optimizer groups with different weight decay settings
        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {'params': nodecay_params, 'weight_decay': 0.0}
        ]

        # Print the number of decayed and non-decayed parameters
        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)
        print(f"num decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} parameters")
        print(f"num non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} parameters")

        # Check if fused AdamW is available and if the device type is CUDA
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and device_type == 'cuda'

        # Define extra arguments for the optimizer
        extra_args = dict(fused=True) if use_fused else dict()

        # Create AdamW optimizer with the given settings
        optimizer = torch.optim.AdamW(optim_groups, lr=learning_rate, betas=betas, **extra_args)

        print(f"using fused AdamW: {use_fused}")

        return optimizer

    def estimate_mfu(self, fwdbwd_per_iter, dt):
        """
        Estimates the model flops utilization (MFU) in units of A100 bfloat16 peak FLOPS.

        This method first calculates the number of flops per iteration using the model's configuration
        parameters and the given fwdbwd_per_iter. It then computes the MFU by comparing the estimated 
        flops with the peak flops of an A100 GPU in bfloat16 mode.

        Args:
            fwdbwd_per_iter (float): The number of forward and backward passes per iteration.
            dt (float): The time duration of the iteration in seconds.

        Returns:
            float: The estimated model flops utilization (MFU).

        Examples:
            >>> gpt = GPT()
            >>> mfu = gpt.estimate_mfu(2, 0.01)
        """
        # Number of parameters in the model
        N = self.get_num_params()

        # Model's configuration
        cfg = self.cfg.gpt

        # Unpack key parameters from the configuration
        L, H, Q, T = cfg.n_layer, cfg.n_head, cfg.n_embd // cfg.n_head, cfg.block_size

        # Estimate the number of floating point operations (flops) per token and per iteration
        flops_per_token = 6 * N + 12 * L * H * Q * T
        flops_per_fwdbwd = flops_per_token * T
        flops_per_iter = flops_per_fwdbwd * fwdbwd_per_iter

        # Compute flops achieved per second
        flops_achieved = flops_per_iter * (1.0 / dt)

        # A100 GPU bfloat16 peak flops is 312 TFLOPS
        flops_promised = 312e12

        # Compute model flops utilization (MFU) as the ratio of achieved flops to peak flops
        mfu = flops_achieved / flops_promised

        return mfu

    @torch.no_grad()
    def generate(self, idx, max_new_tokens, temperature=1.0, top_k=None):
        """
        Generates new tokens conditioned on the input sequence of tokens.

        The method takes a conditioning sequence of indices and completes the sequence
        for a given number of times, feeding the predictions back into the model each time. 
        The model should be in evaluation mode for this method to work properly. 

        Args:
            idx (torch.LongTensor): The input sequence of tokens of shape (b, t) where
                b is the batch size and t is the sequence length.
            max_new_tokens (int): The number of new tokens to generate.
            temperature (float, optional): The temperature factor to scale the output logits.
                Higher values make the outputs more random. Defaults to 1.0.
            top_k (int, optional): The number of top k tokens to consider for the final
                softmax calculation. If None, all tokens are considered. Defaults to None.

        Returns:
            torch.LongTensor: The completed sequence of tokens.

        Examples:
            >>> gpt = GPT()
            >>> idx = torch.LongTensor([[50256, 50256]])
            >>> completed_sequence = gpt.generate(idx, 10, temperature=0.7, top_k=50)
        """
        for _ in range(max_new_tokens):
            # If the sequence context is growing too long, crop it to block size
            idx_cond = idx if idx.size(1) <= self.cfg.gpt.block_size else idx[:, -self.cfg.gpt.block_size:]
            # Forward the model to get the logits for the index in the sequence
            logits, _ = self(idx_cond)
            # Scale the logits at the final step by the desired temperature
            logits = logits[:, -1, :] / temperature
            # Optionally crop the logits to only the top k options
            if top_k is not None:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = -float('Inf')
            # Apply softmax to convert logits to (normalized) probabilities
            probs = F.softmax(logits, dim=-1)
            # Sample from the distribution
            idx_next = torch.multinomial(probs, num_samples=1)
            # Append the sampled index to the running sequence
            idx = torch.cat((idx, idx_next), dim=1)

        return idx
