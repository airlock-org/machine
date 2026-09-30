// Independent of GJS so pytest can exercise focus races with an installed JS runtime.
export const UNKNOWN = {kind: 'unknown', name: 'Unknown VM', color: null};
export const LOCAL = {kind: 'local', name: 'sys-gui', color: null};

export function validateIdentity(value) {
    if (value?.kind === 'local' && value.name === 'sys-gui')
        return {...LOCAL};
    if (value?.kind === 'vm' && /^[A-Za-z][A-Za-z0-9_-]{0,30}$/.test(value.name)) {
        return {kind: 'vm', name: value.name,
            color: /^#[0-9a-f]{6}$/.test(value.color) ? value.color : null};
    }
    return {...UNKNOWN};
}

export class FocusState {
    constructor(render) {
        this._render = render;
        this._revision = 0;
        this._active = true;
    }

    begin(window, suppressed) {
        const revision = ++this._revision;
        this._render(window && !suppressed ? {...UNKNOWN} : null);
        return revision;
    }

    complete(revision, value) {
        if (this._active && revision === this._revision)
            this._render(validateIdentity(value));
    }

    disable() {
        this._active = false;
        ++this._revision;
        this._render(null);
    }
}
