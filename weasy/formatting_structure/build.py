# coding: utf8

#  WeasyPrint converts web documents (HTML, CSS, ...) to PDF.
#  Copyright (C) 2011  Simon Sapin
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as
#  published by the Free Software Foundation, either version 3 of the
#  License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""
Building helpers.

Functions building a correct formatting structure from a DOM document,
including handling of anonymous boxes and whitespace processing.

"""

import re
from . import boxes
from .. import html
from ..css.values import get_single_keyword


GLYPH_LIST_MARKERS = {
    'disc': u'•',  # U+2022, BULLET
    'circle': u'◦',  # U+25E6 WHITE BULLET
    'square': u'▪',  # U+25AA BLACK SMALL SQUARE
}


def build_formatting_structure(document):
    """Build a formatting structure (box tree) from a ``document``."""
    box = dom_to_box(document, document.dom)
    assert box is not None
    box = inline_in_block(box)
    box = block_in_inline(box)
    box = process_whitespace(box)
    return box


def dom_to_box(document, element):
    """Convert a DOM element and its children into a box with children.

    Eg.::

        <p>Some <em>emphasised</em> text.<p>

    gives (not actual syntax)::

        BlockBox[
            TextBox['Some '],
            InlineBox[
                TextBox['emphasised'],
            ],
            TextBox[' text.'],
        ]

    ``TextBox``es are anonymous inline boxes:
    See http://www.w3.org/TR/CSS21/visuren.html#anonymous

    """
    # TODO: should be the used value. When does the used value for `display`
    # differ from the computer value?
    display = get_single_keyword(document.style_for(element).display)
    if display == 'none':
        return None

    result = html.handle_element(document, element)
    if result is not html.DEFAULT_HANDLING:
        # Specific handling for the element. (eg. replaced element)
        return result

    if display in ('block', 'list-item'):
        box = boxes.BlockBox(document, element)
        if display == 'list-item':
            add_list_marker(box)
    elif display == 'inline':
        box = boxes.InlineBox(document, element)
    elif display == 'inline-block':
        box = boxes.InlineBlockBox(document, element)
    else:
        raise NotImplementedError('Unsupported display: ' + display)

    assert isinstance(box, boxes.ParentBox)
    if element.text:
        box.add_child(boxes.TextBox(document, element, element.text))
    for child_element in element:
        # lxml.html already converts HTML entities to text.
        # Here we ignore comments and XML processing instructions.
        if isinstance(child_element.tag, basestring):
            child_box = dom_to_box(document, child_element)
            if child_box is not None:
                box.add_child(child_box)
            # else: child_element had `display: None`
        if child_element.tail:
            box.add_child(boxes.TextBox(
                document, element, child_element.tail))

    return box


def add_list_marker(box):
    """Add a list marker to elements with ``display: list-item``.

    See http://www.w3.org/TR/CSS21/generate.html#lists

    """
    image = box.style.list_style_image
    if get_single_keyword(image) != 'none':
        # surface may be None here too, in case the image is not available.
        surface = box.document.get_image_surface_from_uri(image[0].absoluteUri)
    else:
        surface = None

    if surface is None:
        type_ = get_single_keyword(box.style.list_style_type)
        if type_ == 'none':
            return
        marker = GLYPH_LIST_MARKERS[type_]
        marker_box = boxes.TextBox(box.document, box.element, marker)
    else:
        replacement = html.ImageReplacement(surface)
        marker_box = boxes.ImageMarkerBox(
            box.document, box.element, replacement)

    position = get_single_keyword(box.style.list_style_position)
    if position == 'inside':
        assert not box.children  # Make sure we’re adding at the beggining
        box.add_child(marker_box)
        # U+00A0, NO-BREAK SPACE
        spacer = boxes.TextBox(box.document, box.element, u'\u00a0')
        box.add_child(spacer)
    elif position == 'outside':
        box.outside_list_marker = marker_box
        marker_box.parent = box


def process_whitespace(box):
    """First part of "The 'white-space' processing model".

    See http://www.w3.org/TR/CSS21/text.html#white-space-model

    """
    following_collapsible_space = False
    for child in box.descendants():
        if not (isinstance(child, boxes.TextBox) and child.utf8_text):
            continue

        # TODO: find a way to do less decoding and re-encoding
        text = child.utf8_text.decode('utf8')
        handling = get_single_keyword(child.style.white_space)

        if handling in ('normal', 'nowrap', 'pre-line'):
            text = re.sub('[\t\r ]*\n[\t\r ]*', '\n', text)
        if handling in ('pre', 'pre-wrap'):
            # \xA0 is the non-breaking space
            text = text.replace(' ', u'\xA0')
            if handling == 'pre-wrap':
                # "a line break opportunity at the end of the sequence"
                # \u200B is the zero-width space, marks a line break
                # opportunity.
                text = re.sub(u'\xA0([^\xA0]|$)', u'\xA0\u200B\\1', text)
        elif handling in ('normal', 'nowrap'):
            # TODO: this should be language-specific
            # Could also replace with a zero width space character (U+200B),
            # or no character
            # CSS3: http://www.w3.org/TR/css3-text/#line-break-transform
            text = text.replace('\n', ' ')

        if handling in ('normal', 'nowrap', 'pre-line'):
            text = text.replace('\t', ' ')
            text = re.sub(' +', ' ', text)
            if following_collapsible_space and text.startswith(' '):
                text = text[1:]
            following_collapsible_space = text.endswith(' ')
        else:
            following_collapsible_space = False

        child.utf8_text = text.encode('utf8')
    return box


def inline_in_block(box):
    """Build the structure of lines inside blocks.

    Consecutive inline-level boxes in a block container box are wrapped into a
    line box, itself wrapped into an anonymous block box.

    This line box will be broken into multiple lines later.

    The box tree is changed *in place*.

    This is the first case in
    http://www.w3.org/TR/CSS21/visuren.html#anonymous-block-level

    Eg.::

        BlockBox[
            TextBox['Some '],
            InlineBox[TextBox['text']],
            BlockBox[
                TextBox['More text'],
            ]
        ]

    is turned into::

        BlockBox[
            AnonymousBlockBox[
                LineBox[
                    TextBox['Some '],
                    InlineBox[TextBox['text']],
                ]
            ]
            BlockBox[
                LineBox[
                    TextBox['More text'],
                ]
            ]
        ]

    """
    for child_box in getattr(box, 'children', []):
        inline_in_block(child_box)

    if not isinstance(box, boxes.BlockContainerBox):
        return

    line_box = boxes.LineBox(box.document, box.element)
    children = box.children
    box.empty()
    for child_box in children:
        if isinstance(child_box, boxes.BlockLevelBox):
            if line_box.children:
                # Inlines are consecutive no more: add this line box
                # and create a new one.
                anonymous = boxes.AnonymousBlockBox(box.document, box.element)
                anonymous.add_child(line_box)
                box.add_child(anonymous)
                line_box = boxes.LineBox(box.document, box.element)
            box.add_child(child_box)
        elif isinstance(child_box, boxes.LineBox):
            # Merge the line box we just found with the new one we are making
            for child in child_box.children:
                line_box.add_child(child)
        else:
            line_box.add_child(child_box)
    if line_box.children:
        # There were inlines at the end
        if box.children:
            anonymous = boxes.AnonymousBlockBox(box.document, box.element)
            anonymous.add_child(line_box)
            box.add_child(anonymous)
        else:
            # Only inline-level children: one line box
            box.add_child(line_box)
    return box


def block_in_inline(box):
    """Build the structure of blocks inside lines.

    Inline boxes containing block-level boxes will be broken in two
    boxes on each side on consecutive block-level boxes, each side wrapped
    in an anonymous block-level box.

    This is the second case in
    http://www.w3.org/TR/CSS21/visuren.html#anonymous-block-level

    Eg. if this is given::

        BlockBox[
            LineBox[
                InlineBox[
                    TextBox['Hello.'],
                ],
                InlineBox[
                    TextBox['Some '],
                    InlineBox[
                        TextBox['text']
                        BlockBox[LineBox[TextBox['More text']]],
                        BlockBox[LineBox[TextBox['More text again']]],
                    ],
                    BlockBox[LineBox[TextBox['And again.']]],
                ]
            ]
        ]

    this is returned::

        BlockBox[
            AnonymousBlockBox[
                LineBox[
                    InlineBox[
                        TextBox['Hello.'],
                    ],
                    InlineBox[
                        TextBox['Some '],
                        InlineBox[TextBox['text']],
                    ]
                ]
            ],
            BlockBox[LineBox[TextBox['More text']]],
            BlockBox[LineBox[TextBox['More text again']]],
            AnonymousBlockBox[
                LineBox[
                    InlineBox[
                    ]
                ]
            ],
            BlockBox[LineBox[TextBox['And again.']]],
            AnonymousBlockBox[
                LineBox[
                    InlineBox[
                    ]
                ]
            ],
        ]

    """
    if not isinstance(box, boxes.ParentBox):
        return box

    new_children = []
    changed = False

    for child in box.children:
        if isinstance(child, boxes.LineBox):
            assert len(box.children) == 1, ('Line boxes should have no '
                'siblings at this stage, got %r.' % box.children)
            stack = None
            while 1:
                new_line, block, stack = _inner_block_in_inline(child, stack)
                if block is None:
                    break
                anon = boxes.AnonymousBlockBox(box.document, box.element)
                anon.add_child(new_line)
                new_children.append(anon)
                new_children.append(block_in_inline(block))
                # Loop with the same child and the new stack.
            if new_children:
                # Some children were already added, this became a block
                # context.
                new_child = boxes.AnonymousBlockBox(box.document, box.element)
                new_child.add_child(new_line)
            else:
                # Keep the single line box as-is, without anonymous blocks.
                new_child = new_line
        else:
            # Not in an inline formatting context.
            new_child = block_in_inline(child)

        if new_child is not child:
            changed = True
        new_children.append(new_child)

    if changed:
        new_box = box.copy()
        new_box.empty()
        for new_child in new_children:
            new_box.add_child(new_child)
        return new_box
    else:
        return box


def _add_anonymous_block(box, child):
    """Wrap the child in an AnonymousBlockBox and add it to box."""
    anon_block = boxes.AnonymousBlockBox(box.document, box.element)
    anon_block.add_child(child)
    box.add_child(anon_block)


def _inner_block_in_inline(box, skip_stack=None):
    """Find a block-level box in an inline formatting context.

    If one is found, return ``(new_box, block_level_box, resume_at)``.
    ``new_box`` contains all of ``box`` content before the block-level box.
    ``resume_at`` can be passed as ``skip_stack`` in a new call to
    this function to resume the search just after thes block-level box.

    If no block-level box is found after the position marked by
    ``skip_stack``, return ``(new_box, None, None)``

    """
    new_box = box.copy()
    new_box.empty()
    block_level_box = None
    resume_at = None
    changed = False

    if skip_stack is None:
        skip = 0
    else:
        skip, skip_stack = skip_stack

    for index, child in box.enumerate_skip(skip):
        if isinstance(child, boxes.BlockLevelBox):
            block_level_box = child
            index += 1  # Resume *after* the block
        else:
            if isinstance(child, boxes.InlineBox):
                recursion = _inner_block_in_inline(child, skip_stack)
                new_child, block_level_box, resume_at = recursion
            else:
                if isinstance(child, boxes.ParentBox):
                    # inline-block or inline-table.
                    new_child = block_in_inline(child)
                else:
                    # text or replaced box
                    new_child = child
                # block_level_box is still None.
            if new_child is not child:
                changed = True
            new_box.add_child(new_child)
        if block_level_box is not None:
            resume_at = (index, resume_at)
            break
    else:
        if not (changed or skip):
            new_box = box
    return new_box, block_level_box, resume_at
