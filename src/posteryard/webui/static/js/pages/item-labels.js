// Plain-word explanations of Posteryard's labels and art overrides.

export const LABEL_HELP = {
  'posteryard-next': 'Next art is waiting: the art switches at the next sweep, and the label is then removed. With DRY_RUN on, the label stays.',
  'posteryard-custom': 'Uses the poster you uploaded in Plex as the art, with the title and badges drawn on top. Remove the label to go back to automatic art.',
  'posteryard-ignore': 'Posteryard leaves this title alone. Remove the label and it is rendered fresh.',
  'posteryard-collection': 'A collection Posteryard made for a streaming service.',
};

const SOURCES = {
  command: 'set with a command or the web UI', plex: 'taken from the poster in Plex with the posteryard-custom label',
};

export function sourceText(source) {
  return source ? SOURCES[source] || `set by ${source}` : 'chosen by hand';
}

/** Short badges for an override: custom art and images skipped with Next art. */
export function overrideBadges(override) {
  if (!override) return [];
  const out = [];
  if (override.custom) out.push({ text: 'Custom art', tone: 'accent', title: `Your own image, ${sourceText(override.source)}.` });
  const skipped = (override.skip || []).length;
  if (skipped) {
    out.push({ text: `Skipped ${skipped} ${skipped === 1 ? 'image' : 'images'} with Next art`, tone: 'purple', title: 'Next art moved past these pictures.' });
  }
  return out;
}
