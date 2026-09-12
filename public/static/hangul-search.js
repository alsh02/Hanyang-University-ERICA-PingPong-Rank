// 한글 이름 검색 규칙. app.py의 match_name과 같은 동작을 브라우저에서도 하도록 맞춰 둔 것으로,
// 검색 페이지와 점수판의 선수 선택이 함께 쓴다.
window.HangulSearch = (() => {
    const CHOSUNG = 'ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ';
    const JUNGSUNG = ['ㅏ', 'ㅐ', 'ㅑ', 'ㅒ', 'ㅓ', 'ㅔ', 'ㅕ', 'ㅖ', 'ㅗ', 'ㅗㅏ', 'ㅗㅐ', 'ㅗㅣ', 'ㅛ', 'ㅜ',
                      'ㅜㅓ', 'ㅜㅔ', 'ㅜㅣ', 'ㅠ', 'ㅡ', 'ㅡㅣ', 'ㅣ'];
    const JONGSUNG = ['', 'ㄱ', 'ㄲ', 'ㄱㅅ', 'ㄴ', 'ㄴㅈ', 'ㄴㅎ', 'ㄷ', 'ㄹ', 'ㄹㄱ', 'ㄹㅁ', 'ㄹㅂ', 'ㄹㅅ', 'ㄹㅌ',
                      'ㄹㅍ', 'ㄹㅎ', 'ㅁ', 'ㅂ', 'ㅂㅅ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ'];
    const COMPOUND_JAMO = { 'ㄳ': 'ㄱㅅ', 'ㄵ': 'ㄴㅈ', 'ㄶ': 'ㄴㅎ', 'ㄺ': 'ㄹㄱ', 'ㄻ': 'ㄹㅁ', 'ㄼ': 'ㄹㅂ', 'ㄽ': 'ㄹㅅ',
                            'ㄾ': 'ㄹㅌ', 'ㄿ': 'ㄹㅍ', 'ㅀ': 'ㄹㅎ', 'ㅄ': 'ㅂㅅ', 'ㅘ': 'ㅗㅏ', 'ㅙ': 'ㅗㅐ', 'ㅚ': 'ㅗㅣ',
                            'ㅝ': 'ㅜㅓ', 'ㅞ': 'ㅜㅔ', 'ㅟ': 'ㅜㅣ', 'ㅢ': 'ㅡㅣ' };

    const syllableIndex = (ch) => {
        const code = ch.charCodeAt(0) - 0xAC00;
        return code >= 0 && code < 11172 ? code : -1;
    };
    const toJamo = (text) => Array.from(text, (ch) => {
        const code = syllableIndex(ch);
        if (code < 0) return COMPOUND_JAMO[ch] || ch;
        return CHOSUNG[Math.floor(code / 588)] + JUNGSUNG[Math.floor((code % 588) / 28)] + JONGSUNG[code % 28];
    }).join('').toLowerCase();
    const toChosung = (text) => Array.from(text, (ch) => {
        const code = syllableIndex(ch);
        return code < 0 ? ch : CHOSUNG[Math.floor(code / 588)];
    }).join('');

    // 초성만 입력하면 초성으로, 그 밖에는 '한경' → '한경민'처럼 입력 중인 글자까지 맞춰 본다
    function matchName(name, rawQuery) {
        const query = rawQuery.replace(/\s+/g, '').toLowerCase();
        if (!query) return false;
        name = name.toLowerCase();
        const queryJamo = toJamo(query);
        if (Array.from(queryJamo).every((ch) => CHOSUNG.includes(ch))) {
            return toChosung(name).includes(queryJamo);
        }
        const head = query.slice(0, -1);
        const last = toJamo(query.slice(-1));
        for (let i = 0; i + head.length <= name.length; i++) {
            if (name.startsWith(head, i) && toJamo(name.slice(i + head.length)).startsWith(last)) return true;
        }
        return false;
    }

    return { CHOSUNG, toJamo, toChosung, matchName };
})();
