import typing

import gradio as gr
from typing import List
from .utils import seafoam


class GradioUserInference:
    @staticmethod
    def chat_interface_components(
            sample_func: typing.Callable,
            max_sequence_length: int,
            max_new_tokens: int,
            max_compile_tokens: int
    ):
        """
        The function `chat_interface_components` creates the components for a chat interface, including
        a chat history, message box, buttons for submitting, stopping, and clearing the conversation,
        and sliders for advanced options.
        """

        _max_length = max_sequence_length
        _max_new_tokens = max_new_tokens
        _max_compile_tokens = max_compile_tokens

        with gr.Column("100%"):
            gr.Markdown(
                "# <h1><center style='color:white;'>Powered by "
                "[Detoxio](https://detoxio.ai)</center></h1>",
            )
            history = gr.Chatbot(
                elem_id="Detoxio",
                label="Detoxio",
                container=True,
                height="65vh",
            )
            prompt = gr.Textbox(
                show_label=False, placeholder='Enter Your Prompt Here.', container=False
            )
            with gr.Row():
                submit = gr.Button(
                    value="Run",
                    variant="primary"
                )
                stop = gr.Button(
                    value='Stop'
                )
                clear = gr.Button(
                    value='Clear Conversation'
                )
            with gr.Accordion(open=False, label="Advanced Options"):
                system_prompt = gr.Textbox(
                    value="",
                    show_label=False,
                    label="System Prompt",
                    placeholder='System Prompt',
                    container=False
                )

                max_sequence_length = gr.Slider(
                    value=_max_length,
                    maximum=10000,
                    minimum=1,
                    label='Max Tokens',
                    step=1
                )

                max_new_tokens = gr.Slider(
                    value=_max_new_tokens,
                    maximum=10000,
                    minimum=_max_compile_tokens,
                    label='Max New Tokens',
                    step=_max_compile_tokens
                )

                max_compile_tokens = gr.Slider(
                    value=_max_compile_tokens,
                    maximum=_max_compile_tokens,
                    minimum=_max_compile_tokens,
                    label='Max Compile Tokens',
                    step=_max_compile_tokens
                )

                temperature = gr.Slider(
                    value=0.8,
                    maximum=1,
                    minimum=0.1,
                    label='Temperature',
                    step=0.01
                )
                top_p = gr.Slider(
                    value=0.9,
                    maximum=1,
                    minimum=0.1,
                    label='Top P',
                    step=0.01
                )
                top_k = gr.Slider(
                    value=50,
                    maximum=100,
                    minimum=1,
                    label='Top K',
                    step=1
                )
                repetition_penalty = gr.Slider(
                    value=1.2,
                    maximum=5,
                    minimum=0.1,
                    label='Repetition Penalty'
                )
                greedy = gr.Radio(
                    value=True,
                    label="Do Sample or Greedy Generation"
                )

                mode = gr.Dropdown(
                    choices=["Chat", "Instruct"],
                    value="Chat",
                    label="Mode",
                    multiselect=False
                )

        inputs = [
            prompt,
            history,
            system_prompt,
            mode,
            max_sequence_length,
            max_new_tokens,
            max_compile_tokens,
            greedy,
            temperature,
            top_p,
            top_k,
            repetition_penalty
        ]

        clear.click(fn=lambda: [], outputs=[history])
        sub_event = submit.click(
            fn=sample_func, inputs=inputs, outputs=[prompt, history]
        )
        txt_event = prompt.submit(
            fn=sample_func, inputs=inputs, outputs=[prompt, history]
        )
        stop.click(
            fn=None,
            inputs=None,
            outputs=None,
            cancels=[txt_event, sub_event]
        )

    def sample_gradio(
            self,
            prompt: str,
            history: List[List[str]],
            system_prompt: str | None,
            mode: str,
            max_sequence_length: int,
            max_new_tokens: int,
            max_compile_tokens: int,
            greedy: bool,
            temperature: float,
            top_p: float,
            top_k: int,
            repetition_penalty: float
    ):
        raise NotImplementedError()

    def build_inference(
            self,
            sample_func: typing.Callable,
            max_sequence_length: int,
            max_new_tokens: int,
            max_compile_tokens: int
    ) -> gr.Blocks:
        """
        The function "build_inference" returns a gr.Blocks object that model
        interface components.
        :return: a gr.Blocks object.
        """
        with gr.Blocks(
                theme=seafoam
        ) as block:
            self.chat_interface_components(
                sample_func=sample_func,
                max_sequence_length=max_sequence_length,
                max_new_tokens=max_new_tokens,
                max_compile_tokens=max_compile_tokens
            )
        return block
