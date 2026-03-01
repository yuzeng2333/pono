#include "utils/predicate_reader.h"

#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>

#include "smt-switch/smt.h"
#include "utils/exceptions.h"
#include "utils/logger.h"

using namespace smt;
using namespace std;

namespace pono {

// Minimal JSON helpers for the specific predicate format.
// Handles: [ { "key": "value", ... }, ... ]

// Skip whitespace in a string starting at pos.
static void skip_ws(const string & s, size_t & pos)
{
  while (pos < s.size() && isspace(s[pos])) pos++;
}

// Expect and consume a specific character.
static void expect_char(const string & s, size_t & pos, char c)
{
  skip_ws(s, pos);
  if (pos >= s.size() || s[pos] != c) {
    throw PonoException("predicate_reader: expected '" + string(1, c)
                        + "' at position " + to_string(pos));
  }
  pos++;
}

// Parse a JSON string value (assumes pos is at the opening quote).
static string parse_string(const string & s, size_t & pos)
{
  skip_ws(s, pos);
  if (pos >= s.size() || s[pos] != '"') {
    throw PonoException("predicate_reader: expected '\"' at position "
                        + to_string(pos));
  }
  pos++;  // skip opening quote
  string result;
  while (pos < s.size() && s[pos] != '"') {
    if (s[pos] == '\\') {
      pos++;
      if (pos < s.size()) result += s[pos];
    } else {
      result += s[pos];
    }
    pos++;
  }
  if (pos >= s.size()) {
    throw PonoException("predicate_reader: unterminated string");
  }
  pos++;  // skip closing quote
  return result;
}

// Skip a JSON value (string, number, object, array, bool, null).
static void skip_value(const string & s, size_t & pos)
{
  skip_ws(s, pos);
  if (pos >= s.size()) return;
  char c = s[pos];
  if (c == '"') {
    parse_string(s, pos);
  } else if (c == '{') {
    pos++;
    skip_ws(s, pos);
    if (pos < s.size() && s[pos] == '}') { pos++; return; }
    while (true) {
      parse_string(s, pos);
      expect_char(s, pos, ':');
      skip_value(s, pos);
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ',') { pos++; continue; }
      break;
    }
    expect_char(s, pos, '}');
  } else if (c == '[') {
    pos++;
    skip_ws(s, pos);
    if (pos < s.size() && s[pos] == ']') { pos++; return; }
    while (true) {
      skip_value(s, pos);
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ',') { pos++; continue; }
      break;
    }
    expect_char(s, pos, ']');
  } else {
    // number, bool, null — skip until delimiter
    while (pos < s.size() && s[pos] != ',' && s[pos] != '}' && s[pos] != ']'
           && !isspace(s[pos])) {
      pos++;
    }
  }
}

// Look up an operand name in the transition system.
// Returns the term if found as a named term or state variable symbol.
// If not found and the name looks like an integer constant, creates one.
static Term resolve_operand(const string & name,
                            const TransitionSystem & ts,
                            const SmtSolver & solver)
{
  // First try named_terms (BTOR2 symbol names)
  const auto & named = ts.named_terms();
  auto it = named.find(name);
  if (it != named.end()) {
    return it->second;
  }

  // Also check state variables by their string representation
  for (const auto & sv : ts.statevars()) {
    if (sv->to_string() == name) {
      return sv;
    }
  }

  // Try to parse as an integer constant
  // Need a sort to create the constant — but we don't know the sort yet.
  // Return nullptr and let the caller handle it.
  return nullptr;
}

TermVec read_predicates(const string & json_path,
                        const TransitionSystem & ts,
                        const SmtSolver & solver)
{
  // Read the entire file
  ifstream ifs(json_path);
  if (!ifs.is_open()) {
    throw PonoException("Cannot open predicate file: " + json_path);
  }
  stringstream ss;
  ss << ifs.rdbuf();
  string content = ss.str();

  TermVec predicates;
  size_t pos = 0;

  // Parse top-level array
  expect_char(content, pos, '[');

  skip_ws(content, pos);
  if (pos < content.size() && content[pos] == ']') {
    pos++;
    return predicates;  // empty array
  }

  while (true) {
    // Parse one predicate object
    expect_char(content, pos, '{');

    string pred_type;
    string op1_name;
    string op2_name;

    // Parse key-value pairs
    skip_ws(content, pos);
    while (pos < content.size() && content[pos] != '}') {
      string key = parse_string(content, pos);
      expect_char(content, pos, ':');

      if (key == "predicate_type") {
        pred_type = parse_string(content, pos);
      } else if (key == "operands") {
        // Parse operands sub-object
        expect_char(content, pos, '{');
        skip_ws(content, pos);
        while (pos < content.size() && content[pos] != '}') {
          string okey = parse_string(content, pos);
          expect_char(content, pos, ':');
          string oval = parse_string(content, pos);
          if (okey == "op1") op1_name = oval;
          else if (okey == "op2") op2_name = oval;
          skip_ws(content, pos);
          if (pos < content.size() && content[pos] == ',') pos++;
          skip_ws(content, pos);
        }
        expect_char(content, pos, '}');
      } else {
        // Skip unknown fields (btor2_info, predicate_id, etc.)
        skip_value(content, pos);
      }

      skip_ws(content, pos);
      if (pos < content.size() && content[pos] == ',') pos++;
      skip_ws(content, pos);
    }
    expect_char(content, pos, '}');

    // Construct the predicate term
    if (op1_name.empty()) {
      logger.log(0, "WARNING: predicate missing op1, skipping");
    } else {
      Term op1 = resolve_operand(op1_name, ts, solver);
      if (!op1) {
        logger.log(0, "WARNING: cannot resolve operand '{}', skipping predicate",
                   op1_name);
      } else {
        Term op2 = nullptr;
        if (!op2_name.empty()) {
          op2 = resolve_operand(op2_name, ts, solver);
          if (!op2) {
            // Try parsing as integer constant with op1's sort
            try {
              long val = stol(op2_name);
              op2 = solver->make_term(val, op1->get_sort());
            } catch (const exception &) {
              logger.log(0,
                         "WARNING: cannot resolve operand '{}', "
                         "skipping predicate",
                         op2_name);
            }
          }
        }

        if (op2) {
          PrimOp prim_op;
          if (pred_type == "eq") {
            prim_op = Equal;
          } else if (pred_type == "neq") {
            prim_op = Distinct;
          } else if (pred_type == "lt" || pred_type == "ult") {
            prim_op = BVUlt;
          } else if (pred_type == "lte" || pred_type == "ule") {
            prim_op = BVUle;
          } else if (pred_type == "uge") {
            prim_op = BVUge;
          } else {
            logger.log(
                0, "WARNING: unknown predicate_type '{}', skipping", pred_type);
            goto next_pred;
          }
          Term pred = solver->make_term(prim_op, op1, op2);
          predicates.push_back(pred);
          logger.log(1, "Loaded predicate: {}", pred->to_string());
        }
      }
    }

  next_pred:
    skip_ws(content, pos);
    if (pos < content.size() && content[pos] == ',') {
      pos++;
    } else {
      break;
    }
  }

  expect_char(content, pos, ']');

  return predicates;
}

}  // namespace pono