// -*- mode:c++;tab-width:2;indent-tabs-mode:t;show-trailing-whitespace:t;rm-trailing-spaces:t -*-
// vi: set ts=2 noet:
//
// (c) Copyright Rosetta Commons Member Institutions.
// (c) This file is part of the Rosetta software suite and is made available under license.
// (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
// (c) For more information, see http://www.rosettacommons.org. Questions about this can be
// (c) addressed to University of Washington CoMotion, email: license@uw.edu.

/// @file    utility/io/util.cc
/// @brief   General database input/output utility function definitions.
/// @author  Labonte <JWLabonte@jhu.edu>


// Unit header
#include <utility/io/util.hh>

// Utility headers
#include <utility/exit.hh>
#include <utility/string_util.hh>
#include <utility/file/file_sys_util.hh>
#include <utility/io/izstream.hh>

// C++ headers
#include <cstdint>
#include <cstring>
#include <limits>
#include <locale>
#include <sstream>
#include <string>

// Clinger's fast path, below, needs each multiply or divide carried out as the single IEEE operation
// written. -ffast-math does not promise that: it lets the compiler replace a division by a
// multiplication with a rounded reciprocal. PyRosetta's WebAssembly build, where operator>> is
// slowest, compiles without it. Native Rosetta builds may use it, and Rosetta's gcc release flags,
// -ffast-math -fno-finite-math-only, leave __FAST_MATH__ undefined, so native builds use operator>>.
#if defined( __EMSCRIPTEN__ ) && ! defined( __FAST_MATH__ )
#define UTILITY_IO_READ_NUMBERS_FROM_BUFFER
#endif

namespace utility {
namespace io {

#ifdef UTILITY_IO_READ_NUMBERS_FROM_BUFFER

namespace {

bool
is_space( int const c )
{
	return c == ' ' || c == '\n' || c == '\t' || c == '\r' || c == '\v' || c == '\f';
}

/// @brief  Split a whole token of the form [+-]digits[.digits][(e|E)[+-]digits], where either run of
/// digits around the point may be empty but not both, into its sign, its significand as an integer,
/// and a power of ten. False for any other shape, or for more than 19 significant digits, which could
/// overflow the significand.
bool
split_decimal(
	char const * s,
	bool & negative,
	std::uint64_t & significand,
	int & exponent,
	bool & integer
) {
	negative = ( *s == '-' );
	if ( *s == '-' || *s == '+' ) ++s;
	significand = 0;
	exponent = 0;
	integer = true;
	int digits = 0;
	bool any_digit = false;
	for ( ; *s >= '0' && *s <= '9'; ++s ) {
		any_digit = true;
		if ( significand == 0 && *s == '0' ) continue;
		if ( ++digits > 19 ) return false;
		significand = significand * 10 + ( *s - '0' );
	}
	if ( *s == '.' ) {
		integer = false;
		for ( ++s; *s >= '0' && *s <= '9'; ++s ) {
			any_digit = true;
			--exponent;
			if ( significand == 0 && *s == '0' ) continue;
			if ( ++digits > 19 ) return false;
			significand = significand * 10 + ( *s - '0' );
		}
	}
	if ( ! any_digit ) return false;
	if ( *s == 'e' || *s == 'E' ) {
		integer = false;
		++s;
		bool const negative_exponent = ( *s == '-' );
		if ( *s == '-' || *s == '+' ) ++s;
		if ( *s < '0' || *s > '9' ) return false;
		int power = 0;
		for ( ; *s >= '0' && *s <= '9'; ++s ) {
			if ( power < 10000 ) power = power * 10 + ( *s - '0' );
		}
		exponent += negative_exponent ? -power : power;
	}
	return *s == '\0';
}

double const exact_powers_of_ten[] = {
	1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11,
	1e12, 1e13, 1e14, 1e15, 1e16, 1e17, 1e18, 1e19, 1e20, 1e21, 1e22 };

/// @brief  Convert a token with Clinger's fast path, if its significand is at most 2^53 and its power
/// of ten at most 10^22 either way.
/// @details  Both are then exact in double, so one IEEE multiply or divide rounds correctly, and the
/// result is the one strtod, and so operator>>, gives.
bool
exact_value( char const * token, double & value )
{
	bool negative, integer;
	std::uint64_t significand;
	int exponent;
	if ( ! split_decimal( token, negative, significand, exponent, integer ) ) return false;
	if ( significand > ( std::uint64_t( 1 ) << 53 ) || exponent < -22 || exponent > 22 ) return false;
	double const magnitude = exponent < 0 ? double( significand ) / exact_powers_of_ten[ -exponent ]
		: double( significand ) * exact_powers_of_ten[ exponent ];
	value = negative ? -magnitude : magnitude;
	return true;
}

/// @details  The double nearest the token, rounded to float, is the float nearest the token unless it
/// lies exactly halfway between two floats. Every halfway point is itself a double, so any other double
/// lies on the same side of it as the token. The fast path's doubles are 0 or between 1e-22 and
/// 2^53 * 1e22, all in float's normal range, where a halfway point sets only the highest of the 29
/// significand bits that float drops.
bool
exact_value( char const * token, float & value )
{
	double nearest;
	if ( ! exact_value( token, nearest ) ) return false;
	std::uint64_t bits;
	std::memcpy( &bits, &nearest, sizeof( bits ) );
	if ( ( bits & 0x1fffffff ) == 0x10000000 ) return false;
	value = float( nearest );
	return true;
}

bool
exact_value( char const * token, std::size_t & value )
{
	// operator>> wraps a negative number into an unsigned type; leave any sign to it.
	if ( *token < '0' || *token > '9' ) return false;
	bool negative, integer;
	std::uint64_t significand;
	int exponent;
	if ( ! split_decimal( token, negative, significand, exponent, integer ) ) return false;
	if ( ! integer || significand > std::numeric_limits< std::size_t >::max() ) return false;
	value = std::size_t( significand );
	return true;
}

template< typename T >
std::istream &
read_number_from_buffer( std::istream & in, T & value )
{
	typedef std::istream::traits_type traits;
	std::streambuf * const buffer = in.rdbuf();
	if ( ! in.good() || ! buffer ) {
		in.setstate( std::ios_base::failbit );
		return in;
	}

	traits::int_type c = buffer->sgetc();
	while ( ! traits::eq_int_type( c, traits::eof() ) && is_space( c ) ) c = buffer->snextc();
	if ( traits::eq_int_type( c, traits::eof() ) ) {
		in.setstate( std::ios_base::eofbit | std::ios_base::failbit );
		return in;
	}

	// Numbers in Rosetta's files fit the array. A longer token goes to operator>> whole, up to a length
	// no number needs. Past that it is still read to its end, but not kept, and it fails, so that a file
	// with one endless token cannot exhaust memory.
	std::size_t const max_token_length = 4096;
	char token[ 64 ];
	std::size_t length = 0;
	std::string long_token;
	bool too_long = false;
	do {
		if ( length + 1 < sizeof( token ) ) token[ length++ ] = traits::to_char_type( c );
		else if ( length + long_token.size() < max_token_length ) long_token.push_back( traits::to_char_type( c ) );
		else too_long = true;
		c = buffer->snextc();
	} while ( ! traits::eq_int_type( c, traits::eof() ) && ! is_space( c ) );
	token[ length ] = '\0';

	if ( too_long ) {
		value = T( 0 );
		in.setstate( std::ios_base::failbit );
	} else if ( ! long_token.empty() || std::memchr( token, '\0', length ) || ! exact_value( token, value ) ) {
		// A NUL inside the token would end it early as a C string, so that token comes here too.
		std::istringstream token_stream( std::string( token, length ) + long_token );
		token_stream.imbue( std::locale::classic() );
		// operator>> shows it used the whole token by reaching the end of it.
		if ( ! ( token_stream >> value ) || ! token_stream.eof() ) in.setstate( std::ios_base::failbit );
	}
	if ( traits::eq_int_type( c, traits::eof() ) ) in.setstate( std::ios_base::eofbit );
	return in;
}

}  // namespace

#endif  // UTILITY_IO_READ_NUMBERS_FROM_BUFFER

// General method that opens a file and returns its data as a list of lines after checking for errors.
/// @details  Blank and commented lines are not returned and the file is closed before returning the lines.
/// @author   Labonte <JWLabonte@jhu.edu>
utility::vector1< std::string >
get_lines_from_file_data( std::string const & filename )
{
	using namespace std;
	using namespace utility;
	using namespace utility::file;
	using namespace utility::io;

	// Check if file exists.
	if ( ! file_exists( filename ) ) {
		utility_exit_with_message( "Cannot find database file: '" + filename + "'" );
	}

	// Open file.
	izstream data( ( filename.c_str() ) );
	if ( ! data.good() ) {
		utility_exit_with_message( "Unable to open database file: '" + filename + "'" );
	}

	string line;
	vector1< string > lines;

	while ( getline( data, line ) ) {

		trim( line, " \t\n" );  // Remove leading and trailing whitespace.
		if ( ( line.size() < 1 ) || ( line[ 0 ] == '#' ) ) { continue; }  // Skip comments and blank lines.
		lines.push_back( line );
	}

	data.close();

	return lines;
}

// General method for removing comments from a line read from a database file.
/// @details  Inline comments (indicated with #) are trimmed from the end of a passed string.
/// @author   Labonte <JWLabonte@jhu.edu>
void
remove_inline_comments( std::string & line )
{
	line = line.substr( 0, line.find_first_of( "#" ) );
}

std::istream &
read_number( std::istream & in, double & value )
{
#ifdef UTILITY_IO_READ_NUMBERS_FROM_BUFFER
	return read_number_from_buffer( in, value );
#else
	return in >> value;
#endif
}

std::istream &
read_number( std::istream & in, float & value )
{
#ifdef UTILITY_IO_READ_NUMBERS_FROM_BUFFER
	return read_number_from_buffer( in, value );
#else
	return in >> value;
#endif
}

std::istream &
read_number( std::istream & in, std::size_t & value )
{
#ifdef UTILITY_IO_READ_NUMBERS_FROM_BUFFER
	return read_number_from_buffer( in, value );
#else
	return in >> value;
#endif
}

}  // namespace io
}  // namespace utility
