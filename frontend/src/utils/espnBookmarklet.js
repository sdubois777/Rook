/**
 * Returns the bookmarklet code string.
 * User saves this as a browser bookmark.
 * When clicked on ESPN Fantasy, extracts cookies
 * and redirects to DraftMind automatically.
 */
export function getBookmarkletCode(appUrl) {
  const code = `
    (function() {
      function getCookie(name) {
        var match = document.cookie
          .split('; ')
          .find(function(r) { return r.startsWith(name + '='); });
        return match ? decodeURIComponent(match.split('=')[1]) : null;
      }

      var espn_s2 = getCookie('espn_s2');
      var swid = getCookie('SWID');

      if (!espn_s2 || !swid) {
        alert(
          'ESPN cookies not found.\\n\\n' +
          'Make sure you are logged in to ESPN Fantasy before clicking this.'
        );
        return;
      }

      var leagueMatch = window.location.href.match(/leagueId=(\\d+)/);
      var leagueId = leagueMatch ? leagueMatch[1] : '';

      var url = '${appUrl}/leagues/connect/espn/callback' +
        '?espn_s2=' + encodeURIComponent(espn_s2) +
        '&swid=' + encodeURIComponent(swid);

      if (leagueId) {
        url += '&league_id=' + leagueId;
      }

      window.location.href = url;
    })();
  `.trim()

  return 'javascript:' + encodeURIComponent(code)
}
