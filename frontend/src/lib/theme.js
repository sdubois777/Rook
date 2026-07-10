/**
 * Dark mode theme constants — blue accent.
 */

// Honest copy for the dashboard draft banner when a league has NO scheduled draft
// (LeagueResponse.draft_date is null — e.g. Sleeper/ESPN with no draft set). The banner
// NEVER shows a fake/hardcoded date; it reads the selected league's real synced
// draft_date. Kept here so the wording is changeable in one place.
export const DRAFT_NOT_SCHEDULED = 'Draft date not scheduled'
