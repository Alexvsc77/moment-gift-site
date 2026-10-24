/* Three original, gently looping melodies. Sound starts only after a user click. */
(() => {
  const scores = {
    calm: {
      step: 0.58,
      notes: [72, null, 76, null, 79, 76, 74, null, 72, null, 69, 72, 67, null, 69, null],
      chords: [[60, 64, 67], [57, 60, 64], [53, 57, 60], [55, 59, 62]]
    },
    warm: {
      step: 0.46,
      notes: [67, 72, 76, 72, 69, 72, 76, 79, 76, 72, 69, 72, 67, 69, 72, null],
      chords: [[60, 64, 67], [65, 69, 72], [57, 60, 64], [55, 59, 62]]
    },
    bright: {
      step: 0.38,
      notes: [72, 76, 79, 84, 81, 79, 76, 79, 77, 81, 84, 81, 79, 76, 72, 76],
      chords: [[60, 64, 67], [62, 65, 69], [65, 69, 72], [67, 71, 74]]
    }
  };

  let context = null;
  let timer = null;
  let currentTrack = "";
  let stepIndex = 0;
  let nextTime = 0;

  function playNote(midi, at, duration, volume, shape) {
    const oscillator = context.createOscillator();
    const envelope = context.createGain();
    oscillator.type = shape;
    oscillator.frequency.setValueAtTime(440 * Math.pow(2, (midi - 69) / 12), at);
    envelope.gain.setValueAtTime(0.0001, at);
    envelope.gain.exponentialRampToValueAtTime(volume, at + 0.04);
    envelope.gain.exponentialRampToValueAtTime(0.0001, at + duration);
    oscillator.connect(envelope);
    envelope.connect(context.destination);
    oscillator.start(at);
    oscillator.stop(at + duration + 0.02);
  }

  function schedule() {
    if (!context || !currentTrack) return;
    const score = scores[currentTrack];
    while (nextTime < context.currentTime + 0.35) {
      const note = score.notes[stepIndex];
      if (note !== null) playNote(note, nextTime, score.step * 0.82, 0.055, "triangle");
      if (stepIndex % 4 === 0) {
        const chord = score.chords[stepIndex / 4];
        chord.forEach((pitch) => playNote(pitch, nextTime, score.step * 3.6, 0.012, "sine"));
      }
      nextTime += score.step;
      stepIndex = (stepIndex + 1) % score.notes.length;
    }
  }

  async function stop() {
    if (timer !== null) clearInterval(timer);
    timer = null;
    currentTrack = "";
    if (context) {
      const previous = context;
      context = null;
      await previous.close();
    }
  }

  async function start(track) {
    if (!Object.hasOwn(scores, track)) return false;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return false;
    if (context) await stop();
    context = new AudioContextClass();
    await context.resume();
    currentTrack = track;
    stepIndex = 0;
    nextTime = context.currentTime + 0.05;
    schedule();
    timer = setInterval(schedule, 150);
    return true;
  }

  window.MomentMusic = { start, stop, isPlaying: () => Boolean(context) };
})();
