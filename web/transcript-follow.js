const BOTTOM_TOLERANCE = 2;

/** Follow transcript updates inside one viewport, without scrolling the page. */
export class TranscriptFollow {
  constructor(viewport, {onChange = () => {}, getSelection} = {}) {
    this.viewport = viewport;
    this.onChange = onChange;
    this.getSelection = getSelection || (() => viewport.ownerDocument?.getSelection?.());
    this._following = true;
    this._destroyed = false;
    this._resetPending = false;
    this._before = null;
    this._touchY = null;
    this._listeners = [];
    this._last = this._position();

    this._listen(viewport, 'scroll', () => {
      // Rendering can temporarily change the scroll geometry. Compare the
      // position before the update, not an intermediate layout's new bottom.
      if (!this._before) this._observePosition();
    });
    this._listen(viewport, 'wheel', event => {
      if (!event.defaultPrevented && !event.ctrlKey && event.deltaY < 0) this.pause();
    }, {passive:true});
    this._listen(viewport, 'keydown', event => {
      if (event.defaultPrevented || event.target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) return;
      if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)
          || (event.shiftKey && [' ', 'Spacebar'].includes(event.key))) this.pause();
    });
    this._listen(viewport, 'touchstart', event => {
      this._touchY = event.touches?.length === 1 ? event.touches[0].clientY : null;
    }, {passive:true});
    this._listen(viewport, 'touchmove', event => {
      const y = event.touches?.length === 1 ? event.touches[0].clientY : null;
      if (!event.defaultPrevented && this._touchY !== null && y !== null && y > this._touchY) this.pause();
      this._touchY = y;
    }, {passive:true});
    for (const type of ['touchend', 'touchcancel']) {
      this._listen(viewport, type, () => { this._touchY = null; }, {passive:true});
    }
    this._listen(viewport.ownerDocument, 'selectionchange', () => {
      if (this._hasSelection()) this.pause();
    });
  }

  get following() { return this._following; }

  _listen(target, type, listener, options) {
    if (!target?.addEventListener) return;
    target.addEventListener(type, listener, options);
    this._listeners.push(() => target.removeEventListener(type, listener, options));
  }

  _position() {
    const height = Math.max(0, Number(this.viewport.clientHeight) || 0);
    const bottom = Math.max(0, (Number(this.viewport.scrollHeight) || 0) - height);
    const top = Math.max(0, Number(this.viewport.scrollTop) || 0);
    return {top, bottom, atBottom:bottom - top <= BOTTOM_TOLERANCE,
      visible:height > 0 && !this.viewport.hidden && this.viewport.isConnected !== false};
  }

  _hasSelection() {
    try {
      const selection = this.getSelection?.();
      if (!selection || selection.isCollapsed !== false) return false;
      if (this.viewport.contains(selection.anchorNode) || this.viewport.contains(selection.focusNode)) return true;
      for (let index = 0; index < (selection.rangeCount || 0); index += 1) {
        if (selection.getRangeAt(index).intersectsNode?.(this.viewport)) return true;
      }
    } catch { /* A selection can outlive the DOM nodes from an earlier view. */ }
    return false;
  }

  _setFollowing(value) {
    if (this._following === value) return;
    this._following = value;
    this.onChange({following:value});
  }

  _observePosition() {
    if (this._destroyed) return;
    const position = this._position();
    if (this._hasSelection() || (!this._resetPending && position.visible && !position.atBottom
        && position.top < this._last.top - BOTTOM_TOLERANCE)) this.pause();
    this._last = position;
  }

  _scrollToBottom() {
    const position = this._position();
    if (!position.visible) { this._last = position; return; }
    // Store the target before assigning scrollTop: browsers may deliver the
    // resulting scroll event now or after a later render/resume. There is no
    // stale "ignore the next event" flag that could hide a real upward scroll.
    this._last = {...position, top:position.bottom, atBottom:true};
    if (Math.abs(position.top - position.bottom) > BOTTOM_TOLERANCE) {
      this.viewport.scrollTop = position.bottom;
    }
    this._last = this._position();
  }

  /** Changing scope does not move the old view; its next changed render follows. */
  reset({follow = true} = {}) {
    if (this._destroyed) return;
    this._before = null;
    this._resetPending = true;
    this._touchY = null;
    this._last = this._position();
    this._setFollowing(follow === true);
  }

  beforeUpdate() {
    if (this._destroyed) return;
    this._observePosition();
    this._before = {...this._last, following:this.following};
  }

  afterUpdate({changed = false} = {}) {
    if (this._destroyed) return;
    const before = this._before;
    this._before = null;
    this._resetPending = false;
    if (this._hasSelection()) this.pause();
    if (changed && this.following && (!before || before.following)) this._scrollToBottom();
    else this._last = this._position();
  }

  pause() {
    if (!this._destroyed) this._setFollowing(false);
  }

  /** Explicit latest-text navigation takes effect immediately, including paused views. */
  resume() {
    if (this._destroyed) return;
    this._setFollowing(true);
    this._scrollToBottom();
    if (this._before) this._before.following = true;
  }

  destroy() {
    if (this._destroyed) return;
    this._destroyed = true;
    for (const remove of this._listeners) remove();
    this._listeners = [];
    this._before = null;
    this._touchY = null;
  }
}
