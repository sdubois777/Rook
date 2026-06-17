/**
 * Yahoo Snake Draft Room content script.
 *
 * STATUS: STUB — wires the snake DOM observer (yahoo_snake_draft_observer.mjs),
 * which only logs the draft container's text. TO COMPLETE: join a Yahoo snake
 * mock draft, read this script's console output, and map the selectors
 * (pick number, on-the-clock team, available board) into a real poller that
 * relays pick events via POST /draft/event — mirroring yahoo_draft.js for
 * auction. The snake draft URL pattern may differ from the auction
 * draftclient/* path; verify it during that session.
 */
import { initSnakeObserver } from './yahoo_snake_draft_observer.mjs'

initSnakeObserver(window, document)
