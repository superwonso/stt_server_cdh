'use client';
import { useEffect, useRef, useState } from 'react';
import type { GenerateResult, GenerateRequest } from './imports';
const CHANNEL = 'yeobaek-study-v1';
export function useStudyBridge() {
  const [scope, setScope] = useState<string | null>(null);
  const [active, setActive] = useState(true);
  const waiting = useRef(
    new Map<
      string,
      {
        resolve: (data: GenerateResult) => void;
        reject: (error: Error) => void;
        timer: ReturnType<typeof setTimeout>;
      }
    >(),
  );
  useEffect(() => {
    const pending = waiting.current;
    if (window.parent === window) return;
    const send = (data: object) =>
      window.parent.postMessage({ channel: CHANNEL, ...data }, window.location.origin);
    const receive = (event: MessageEvent) => {
      if (
        event.source !== window.parent ||
        event.origin !== window.location.origin ||
        event.data?.channel !== CHANNEL
      )
        return;
      if (event.data.type === 'context') {
        setScope(
          typeof event.data.scope === 'string' && /^[a-f0-9]{64}$/.test(event.data.scope)
            ? event.data.scope
            : null,
        );
        setActive(event.data.active === true);
      }
      if (event.data.type === 'generated') {
        const item = pending.get(event.data.requestId);
        if (!item) return;
        clearTimeout(item.timer);
        pending.delete(event.data.requestId);
        if (event.data.error) item.reject(new Error(String(event.data.error)));
        else if (Array.isArray(event.data.questions))
          item.resolve({
            questions: event.data.questions,
            warnings: Array.isArray(event.data.warnings) ? event.data.warnings : [],
          });
        else item.reject(new Error('문제 생성 결과를 확인하지 못했어요.'));
      }
    };
    window.addEventListener('message', receive);
    const observer = new ResizeObserver(() =>
      send({ type: 'height', height: Math.ceil(document.body.getBoundingClientRect().height) }),
    );
    observer.observe(document.body);
    let lastInteraction = 0;
    const interaction = () => {
      if (Date.now() - lastInteraction > 1000) {
        lastInteraction = Date.now();
        send({ type: 'interaction' });
      }
    };
    window.addEventListener('pointerdown', interaction);
    window.addEventListener('keydown', interaction);
    send({ type: 'ready' });
    return () => {
      observer.disconnect();
      window.removeEventListener('message', receive);
      window.removeEventListener('pointerdown', interaction);
      window.removeEventListener('keydown', interaction);
      for (const item of pending.values()) {
        clearTimeout(item.timer);
        item.reject(new Error('학습 화면이 닫혔어요.'));
      }
      pending.clear();
    };
  }, []);
  const generate = (request: GenerateRequest): Promise<GenerateResult> =>
    new Promise((resolve, reject) => {
      if (!scope || !active) {
        reject(new Error('여백에 로그인하고 다시 시도해 주세요.'));
        return;
      }
      const timer = setTimeout(() => {
        waiting.current.delete(request.requestId);
        reject(new Error('아직 결과를 받지 못했어요. 같은 요청으로 다시 확인해 주세요.'));
      }, 190000);
      waiting.current.set(request.requestId, { resolve, reject, timer });
      window.parent.postMessage(
        { channel: CHANNEL, type: 'generate', ...request },
        window.location.origin,
      );
    });
  return { scope, active, generate };
}
