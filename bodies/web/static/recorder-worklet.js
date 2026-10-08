/*
 * recorder-worklet.js — captures raw PCM from the microphone.
 *
 * Runs on the audio thread and posts each 128-sample block to the page.
 * This is an AudioWorklet rather than the older ScriptProcessorNode: the
 * ScriptProcessorNode is deprecated and is dropped or glitched under load
 * in current browsers, and dropping samples here silently corrupts the
 * WAV we hand to the transcriber.
 *
 * The block must be copied before posting. The input array is reused by
 * the audio thread between calls, so posting it directly would hand the
 * main thread a buffer that changes underneath it.
 */
class AuraRecorder extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length) {
      this.port.postMessage(input[0].slice(0));
    }
    return true;
  }
}

registerProcessor("aura-recorder", AuraRecorder);
