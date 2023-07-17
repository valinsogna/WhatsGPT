# Third party imports
import tiktoken
from torch.nn import functional as F
import torch

# Local application imports
from ..config import Config as cfg
from ..base import BaseGram

class GramSampler(BaseGram):
    """A class for generating text samples from a GPT-2 model.

    This class inherits from the BaseGram class and adds methods for generating text
    samples from a GPT-2 model.

    Attributes:
        x (tensor): The tensor representation of the input text.
        y (tensor): The tensor representation of the generated text.
        enc (Tokenizer): The tokenizer used to encode and decode text.
    """

    def __init__(self, file=None, **kwargs):
        """Initializes the GramSampler with a GPT-2 model and a tokenizer.

        The model and tokenizer are passed as keyword arguments.

        Args:
            file (str, optional): The path to the file containing the initial text. Defaults to None.
            **kwargs: Keyword arguments for the BaseGram constructor.
        """

        super().__init__(**kwargs)  # Call the constructor of the BaseGram class.
        self._encoding_and_encode_prompt(file)  # Initialize the tokenizer and encode the initial text.

    def _encoding_and_encode_prompt(self, file=None):
        """Initializes the tokenizer and encodes the initial text.

        Args:
            file (str, optional): The path to the file containing the initial text. Defaults to None.
        """

        # Get the tokenizer for the GPT-2 model.
        self.enc = tiktoken.get_encoding("gpt2")

        # Define encoding and decoding functions.
        self.encode = lambda s: self.enc.encode(s, allowed_special={""})
        self.decode = lambda l: self.enc.decode(l)

        # If no file is provided, encode the default starting text.
        if file is None:
            start_ids = self.encode(cfg.sampling.start)
            self.x = torch.tensor(start_ids, dtype=torch.long, device=self.device)[None, ...]
        else:  # If a file is provided, read the file and encode its content.
            with open(file, 'r', encoding='utf-8') as f:
                text = f.read()

            # Append the user's name at the end of the text.
            user = cfg.sampling.user
            if not text.endswith('\n'):
                text += '\n'  # make sure there's a newline before the user's name
            text += f"{user}: "

            # Encode the text and create a tensor.
            encoded_text = self.encode(text)
            self.x = torch.tensor(encoded_text, dtype=torch.long, device=self.device)[None, ...]

    def generate(self, temperature, top_k):
        """Generates a text sample.

        Args:
            temperature (float): The temperature for the generation process. Higher values result in more random outputs.
            top_k (int): The number of top predictions to consider for each step in the generation process.

        Returns:
            str: The generated text sample.
        """

        # Perform the text generation process.
        with torch.no_grad():  # No need to track gradients in the generation process.
            with self.ctx:  # Context for the generation process (e.g., for using a GPU).
                # Generate text using the GPT-2 model.
                self.y = self.model.sample(self.x,
                                        max_new_tokens=cfg.sampling.max_new_tokens,
                                        temperature=temperature,
                                        top_k=top_k)

                # Subtract the length of the input tokens to only keep the generated ones.
                generated_tokens = self.y[0].tolist()[len(self.x[0]):]

                # Decode the generated tokens into text.
                response = self.decode(generated_tokens)

                # Split the response by newline characters to isolate messages.
                messages = response.split('\n')

                # Only keep the first message.
                first_message = messages[0]

                # Return the first message.
                return first_message
