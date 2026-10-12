import rulerData from '../../data/rulers.json';
import defaultOverrides from '../../data/alias-overrides.json';
import type { AnswerPart, Country, QuestionType } from './types';

export const DATA_VERSION = 2 as const;
export const COUNTRIES: readonly Country[] = [
  '고구려',
  '백제',
  '신라',
  '통일신라',
  '발해',
  '고려',
  '조선',
  '대한제국',
];
export interface LegacyQuestionLike {
  answer: string;
  type: QuestionType;
  id?: string;
  chapter?: string;
  sub?: string;
  question?: string;
  answerParts?: unknown;
  aliases?: unknown;
  dataVersion?: unknown;
}
export interface StructuredAnswer {
  answer: string;
  answerParts: AnswerPart[];
  aliases: string[];
  type: QuestionType;
  dataVersion: typeof DATA_VERSION;
}
export type AliasOverrides = Record<string, string[]>;
type Upgraded<T> = Omit<T, keyof StructuredAnswer> & StructuredAnswer;
type Ruler = { name: string; country: Country; aliases: string[] };
const rulers = rulerData as Ruler[];
const clean = (value: string) =>
  value
    .normalize('NFC')
    .toLocaleLowerCase('ko-KR')
    .replace(/[\s\p{P}\p{S}]+/gu, '');
const escapePattern = (value: string) =>
  value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s*');
const names = [...new Set(rulers.flatMap((ruler) => [ruler.name, ...ruler.aliases]))].sort(
  (a, b) => b.length - a.length,
);
const countriesPattern = [...COUNTRIES]
  .sort((a, b) => b.length - a.length)
  .map(escapePattern)
  .join('|');
const pattern = `(?<![가-힣])(?:(?<country>${countriesPattern})(?:\\s*/\\s*|\\s+))?(?<name>${names.map(escapePattern).join('|')})(?![가-힣])`;
const parentheticalCountryPattern = new RegExp(
  `(?<![가-힣])(${countriesPattern})\\s*[（(]\\s*$`,
  'u',
);

function parentheticalCountryBefore(value: string): Country | undefined {
  return parentheticalCountryPattern.exec(value)?.[1] as Country | undefined;
}

function countryAlreadyWritten(parts: readonly AnswerPart[], index: number): Country | undefined {
  const previous = parts[index - 1];
  return previous?.kind === 'text' ? parentheticalCountryBefore(previous.value) : undefined;
}

function matchingRulers(name: string): Ruler[] {
  return rulers.filter((ruler) =>
    [ruler.name, ...ruler.aliases].some((item) => clean(item) === clean(name)),
  );
}

function countryFromContext(question: Pick<LegacyQuestionLike, 'chapter' | 'sub'>): Country | null {
  const chapter = question.chapter ?? '',
    sub = question.sub ?? '';
  if (/^(?:Ⅲ[-–]|고려(?:\s|$))/.test(chapter)) return '고려';
  if (/^(?:Ⅳ[-–]|조선(?:\s|$))/.test(chapter)) return '조선';
  if (/^Ⅱ-1/.test(chapter)) {
    const country = /^(고구려|백제|신라)\s*:/.exec(sub)?.[1];
    if (country) return country as Country;
  }
  if (/^Ⅱ-2/.test(chapter) && /^통일신라(?:\s|:)/.test(sub)) return '통일신라';
  return null;
}

function parseParts(question: LegacyQuestionLike): AnswerPart[] {
  if (question.type === 'sequence') return [{ kind: 'text', value: question.answer }];
  const parts: AnswerPart[] = [];
  let offset = 0;
  for (const match of question.answer.matchAll(new RegExp(pattern, 'gu'))) {
    const name = match.groups!.name;
    const candidates = matchingRulers(name);
    const parentheticalCountry = parentheticalCountryBefore(question.answer.slice(0, match.index!));
    const explicit = (match.groups!.country as Country | undefined) ?? parentheticalCountry;
    // An explicitly incompatible country is source information, never silently rewritten.
    // It remains visible as text while the ruler is flagged for review.
    const explicitValid = explicit && candidates.some((ruler) => ruler.country === explicit);
    let start = match.index!;
    if (match.groups!.country && !explicitValid) start += match[0].length - name.length;
    if (start > offset) parts.push({ kind: 'text', value: question.answer.slice(offset, start) });
    const distinct = [...new Set(candidates.map((ruler) => ruler.country))];
    const context = countryFromContext(question);
    const country = explicitValid
      ? explicit
      : explicit
        ? null
        : distinct.length === 1
          ? distinct[0]
          : context && distinct.includes(context)
            ? context
            : null;
    parts.push({ kind: 'ruler', name, country, ...(country ? {} : { countryAmbiguous: true }) });
    offset = match.index! + match[0].length;
  }
  if (offset < question.answer.length)
    parts.push({ kind: 'text', value: question.answer.slice(offset) });
  return parts.length ? parts : [{ kind: 'text', value: question.answer }];
}

function pureParts(parts: readonly AnswerPart[]): string {
  return parts.map((part) => (part.kind === 'text' ? part.value : part.name)).join('');
}

/** Only validated metadata can restore a country removed from a prior explicit display. */
function retainedParts(value: unknown, answer: string): AnswerPart[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > 256) return null;
  const result: AnswerPart[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
    if (item.kind === 'text' && typeof item.value === 'string') {
      result.push({ kind: 'text', value: item.value });
    } else if (
      item.kind === 'ruler' &&
      typeof item.name === 'string' &&
      (item.country === null || COUNTRIES.includes(item.country))
    ) {
      const matches = matchingRulers(item.name);
      if (
        !matches.length ||
        (item.country && !matches.some((ruler) => ruler.country === item.country))
      )
        return null;
      result.push({
        kind: 'ruler',
        name: item.name,
        country: item.country,
        ...(item.country ? {} : { countryAmbiguous: true }),
      });
    } else return null;
  }
  return pureParts(result) === answer ? result : null;
}

function canonicalParts(question: LegacyQuestionLike): AnswerPart[] {
  const parsed = parseParts(question);
  if (question.type === 'sequence') return parsed;
  const retained = retainedParts(question.answerParts, question.answer);
  // Reuse country hints only if both tokenization and pure text agree. Never allow
  // an imported part to turn a building, book, or country-only answer into a ruler.
  if (!retained || retained.length !== parsed.length) return parsed;
  return parsed.map((part, index) => {
    const old = retained[index];
    // Source country still present before a parenthesis outranks stale imported
    // metadata as well as the chapter (e.g. 고려(성종) in a Joseon chapter).
    if (countryAlreadyWritten(parsed, index)) return part;
    if (part.kind === 'ruler' && old.kind === 'ruler' && part.name === old.name && old.country)
      return { kind: 'ruler', name: part.name, country: old.country };
    return part;
  });
}

export function renderAnswer(
  question: Pick<LegacyQuestionLike, 'answer'> & Partial<LegacyQuestionLike>,
  options: { showCountry: boolean },
): string {
  const parts = canonicalParts({ ...question, type: question.type ?? 'short' });
  return parts
    .map((part, index) =>
      part.kind === 'text'
        ? part.value
        : `${options.showCountry && part.country && !part.countryAmbiguous && countryAlreadyWritten(parts, index) !== part.country ? `${part.country} ` : ''}${part.name}`,
    )
    .join('');
}

/** Top-level separators only; commas inside parenthetical names stay together. */
function splitRequired(answer: string): string[] {
  let depth = 0,
    current = '';
  const result: string[] = [];
  for (const ch of answer) {
    if (ch === '(' || ch === '（') depth++;
    if (ch === ')' || ch === '）') depth = Math.max(0, depth - 1);
    if (depth === 0 && /[,，;；/\n]/.test(ch)) {
      if (current.trim()) result.push(current.trim());
      current = '';
    } else current += ch;
  }
  if (current.trim()) result.push(current.trim());
  return result;
}

function parentheticalVariants(answer: string): string[] {
  const result = [answer];
  const main = answer.replace(/[（(][^()（）]*[)）]/g, '').trim();
  if (main) result.push(main);
  for (const match of answer.matchAll(/[（(]([^()（）]*)[)）]/g)) {
    const inside = match[1].trim().replace(/^(?:본명|별칭|호|자)\s*[:：]\s*/, '');
    if (inside.length >= 2 && /\p{L}/u.test(inside) && !/^[\d\s.,~〜\-–—년월일세기]+$/.test(inside))
      result.push(inside);
  }
  return result;
}

function structuralVariants(question: LegacyQuestionLike): string[] {
  let results = [''];
  const parts = canonicalParts(question);
  for (const [index, part] of parts.entries()) {
    const names =
      part.kind === 'text'
        ? [part.value]
        : [
            ...new Set([
              part.name,
              ...matchingRulers(part.name)
                .filter((ruler) => !part.country || ruler.country === part.country)
                .flatMap((ruler) => [ruler.name, ...ruler.aliases]),
            ]),
          ];
    const variants =
      part.kind === 'text' ||
      !part.country ||
      part.countryAmbiguous ||
      countryAlreadyWritten(parts, index) === part.country
        ? names
        : names.flatMap((name) => [name, `${part.country} ${name}`, `${name} ${part.country}`]);
    results = results.flatMap((prefix) => variants.map((suffix) => prefix + suffix)).slice(0, 128);
  }
  return results;
}

/** Keep the surrounding required answer intact while allowing country/name order. */
function countryParentheticalVariants(answer: string): string[] {
  const pair = new RegExp(
    `(?<![가-힣])(?<country>${countriesPattern})\\s*[（(]\\s*(?<name>${names.map(escapePattern).join('|')})\\s*[)）]`,
    'gu',
  );
  let results = [''],
    offset = 0;
  for (const match of answer.matchAll(pair)) {
    const country = match.groups!.country as Country,
      name = match.groups!.name;
    if (!matchingRulers(name).some((ruler) => ruler.country === country)) continue;
    const prefix = answer.slice(offset, match.index!);
    const variants = [
      match[0],
      `${country}/${name}`,
      `${name}/${country}`,
      `${country} ${name}`,
      `${name} ${country}`,
    ];
    results = results
      .flatMap((result) => variants.map((variant) => result + prefix + variant))
      .slice(0, 128);
    offset = match.index! + match[0].length;
  }
  return results.map((result) => result + answer.slice(offset));
}

function validAliases(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 128) return [];
  let size = 0;
  return value.filter((item): item is string => {
    if (
      typeof item !== 'string' ||
      !item.trim() ||
      item.length > 6000 ||
      /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(item)
    )
      return false;
    size += item.length;
    return size <= 32000;
  });
}

/** Full-answer spellings only. Required components never become standalone aliases. */
export function getStructuredAliases(question: LegacyQuestionLike): string[] {
  if (question.type === 'sequence') return [];
  const parts = canonicalParts(question);
  const pure = pureParts(parts);
  const variants = structuralVariants({ ...question, answer: pure, answerParts: parts })
    .flatMap(countryParentheticalVariants)
    .slice(0, 128);
  const required = splitRequired(pure);
  const multiple = question.type === 'multi' && required.length > 1;
  const forbidden = new Set<string>();
  if (multiple) {
    for (const component of required) {
      for (const variant of structuralVariants({
        ...question,
        answer: component,
        type: 'short',
        answerParts: undefined,
      }).flatMap(countryParentheticalVariants)) {
        parentheticalVariants(variant).forEach((item) => forbidden.add(clean(item)));
      }
    }
  }
  const generated = variants.flatMap((variant) =>
    multiple
      ? [variant, splitRequired(variant).join(' '), splitRequired(variant).join('')]
      : parentheticalVariants(variant),
  );
  return [
    ...new Set(
      [...generated, ...validAliases(question.aliases)].filter(
        (item) => clean(item) && !forbidden.has(clean(item)),
      ),
    ),
  ].slice(0, 128);
}

export function enrichQuestion<T extends LegacyQuestionLike>(
  question: T,
  overrides: AliasOverrides = defaultOverrides,
): Upgraded<T> {
  const answerParts = canonicalParts(question);
  const answer = pureParts(answerParts);
  // An original country/ruler pair contains one answer once its country is metadata.
  const type =
    question.type === 'multi' && answer !== question.answer && splitRequired(answer).length === 1
      ? 'short'
      : question.type;
  const aliases = [
    ...validAliases(question.aliases),
    ...validAliases(
      question.id && Object.prototype.hasOwnProperty.call(overrides, question.id)
        ? overrides[question.id]
        : [],
    ),
  ];
  const structured = { ...question, answer, answerParts, type, aliases, dataVersion: DATA_VERSION };
  return { ...structured, aliases: getStructuredAliases(structured) } as Upgraded<T>;
}

export const upgradeQuestion = enrichQuestion;

export function inspectRulerCandidates(question: LegacyQuestionLike): {
  ambiguous: string[];
  unknown: string[];
} {
  if (question.type === 'sequence') return { ambiguous: [], unknown: [] };
  const parts = canonicalParts(question);
  const ambiguous = parts
    .filter((part) => part.kind === 'ruler' && !part.country)
    .map((part) => (part.kind === 'ruler' ? part.name : ''));
  const unknown = parts
    .filter((part) => part.kind === 'text')
    .flatMap((part) =>
      part.kind === 'text'
        ? [...part.value.matchAll(/(?<![가-힣])[가-힣]{1,12}(?:왕|여왕)(?![가-힣])/gu)].map(
            (match) => match[0],
          )
        : [],
    );
  // Broad source headings explicitly include non-ruler people. Report them for
  // review instead of turning them into rulers simply because of that heading.
  if (
    !parts.some((part) => part.kind === 'ruler') &&
    /왕(?:\(또는 인물\))?을 쓰세요/.test(question.sub ?? '')
  ) {
    unknown.push(...splitRequired(question.answer));
  }
  return { ambiguous: [...new Set(ambiguous)], unknown: [...new Set(unknown)] };
}
